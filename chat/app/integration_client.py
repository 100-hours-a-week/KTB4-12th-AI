"""Standalone Backend client: Python 3.10+ and httpx; token in AI_SERVICE_TOKEN.
Private state files preserve IDs/message. No automatic model retries.
"""
import argparse,copy,json,os,sys,uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx

TERMINAL={'COMPLETED','FAILED','CANCELLED'}
class RecoveryNeeded(RuntimeError):pass

def events(lines):
    """httpx iter_lines handles split UTF-8/CRLF; data may span several lines."""
    frame,data={},[]
    for line in lines:
        if line=='':
            if data:
                try:yield int(frame['id']),frame['event'],json.loads('\n'.join(data))
                except (KeyError,ValueError,TypeError) as exc:raise RecoveryNeeded('Malformed SSE frame; resume from the saved snapshot') from exc
            frame,data={},[]
        elif not line.startswith(':'):
            key,separator,value=line.partition(':')
            if separator and value.startswith(' '):value=value[1:]
            if key=='data':data.append(value)
            elif key in {'id','event'}:frame[key]=value
    # An incomplete frame never advances the saved cursor.

def save_state(path,state,create=False):
    path=Path(path);target=path if create else path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as file:json.dump(state,file,ensure_ascii=False,indent=2)
        if not create:os.replace(target,path)
    finally:
        if not create:target.unlink(missing_ok=True)

def root_path(state):
    return ('/v1/chat/test/conversations/' if state.get('protocolFixture') else '/v1/chat/conversations/')+state['conversationId']

def request_body(state):
    return {'message':state['message'],**{k:state[k] for k in ('model','recipientContext','preparedContext') if k in state}}

class BackendClient:
    def __init__(self,http,save=lambda state:None,report=lambda event,data:None):
        self.http,self.save,self.report=http,save,report
    def json(self,method,path,**kwargs):
        response=self.http.request(method,path,**kwargs);response.raise_for_status();return response.json()
    def snapshot(self,state,turn):
        if turn['turnId']!=state['turnId']:raise RecoveryNeeded('Turn identity mismatch')
        state['turn']=copy.deepcopy(turn);self.save(state);self.report('snapshot',turn)
    def resume(self,state):
        root=root_path(state)
        conversation=self.json('PUT',root,json={k:state[k] for k in ('senderUserId','recipientUserId')})['data']
        turn=next((t for t in conversation['turns'] if t['turnId']==state['turnId']),None)
        if turn is None:turn=self.json('PUT',root+'/turns/'+state['turnId'],json=request_body(state))['data']
        if turn['message']!=state['message']:raise RecoveryNeeded('Stored message differs from the original request')
        self.snapshot(state,turn)
        if turn['status'] in TERMINAL:return turn
        cursor=turn['lastEventId']
        with self.http.stream('GET',root+'/turns/'+state['turnId']+'/events',params={'afterEventId':cursor},headers={'Accept':'text/event-stream'}) as response:
            response.raise_for_status()
            if response.headers.get('content-type','').split(';')[0]!='text/event-stream':raise RecoveryNeeded('Expected text/event-stream')
            for ident,event,data in events(response.iter_lines()):
                if ident<=cursor:continue
                if ident!=cursor+1:raise RecoveryNeeded('SSE ID gap; resume to restore the complete snapshot')
                next_turn=copy.deepcopy(state['turn'])
                if event=='turn.finished':
                    if data['turnId']!=state['turnId'] or data['lastEventId']!=ident or data['status'] not in TERMINAL:raise RecoveryNeeded('Invalid terminal snapshot')
                    next_turn=data
                elif event=='progress':next_turn['progressMessage']=data['message']
                elif event=='answer.delta':
                    next_turn.pop('progressMessage',None)
                    if next_turn['blocks'] and next_turn['blocks'][-1]['type']=='text':next_turn['blocks'][-1]['text']+=data['delta']
                    else:next_turn['blocks'].append({'type':'text','text':data['delta']})
                elif event=='cards':next_turn['blocks'].append({'type':'cards','products':data['products']})
                else:raise RecoveryNeeded('Unknown SSE event')
                next_turn['lastEventId']=ident;state['turn']=next_turn;self.save(state);cursor=ident;self.report(event,data)
                if event=='turn.finished':return next_turn
        raise RecoveryNeeded('Stream ended before turn.finished; run resume with the same state file')
    def status(self,state):
        turn=self.json('GET',root_path(state)+'/turns/'+state['turnId'])['data'];self.snapshot(state,turn);return turn
    def retry(self,state):
        turn=self.json('PUT',root_path(state)+'/turns/'+state['turnId'],json=request_body(state))['data'];self.snapshot(state,turn);return turn
    def replay(self,state,after=0):
        with self.http.stream('GET',root_path(state)+'/turns/'+state['turnId']+'/events',params={'afterEventId':after},headers={'Accept':'text/event-stream'}) as response:
            response.raise_for_status()
            for ident,event,data in events(response.iter_lines()):self.report(event,{'id':ident,'payload':data})
    def cancel(self,state):
        # Never create a missing conversation or question while cancelling.
        turn=self.json('PUT',root_path(state)+'/turns/'+state['turnId']+'/cancellation')['data']
        self.snapshot(state,turn);return turn

def validate_state(state,base):
    if state['baseUrl']!=base:raise ValueError('AI_CHAT_URL differs from the saved request')
    for key in ('conversationId','turnId'):
        ident=uuid.UUID(state[key])
        if ident.version!=4 or str(ident)!=state[key]:raise ValueError('Invalid saved UUID v4')
    for key in ('senderUserId','recipientUserId'):
        if type(state[key]) is not int or not 1<=state[key]<=2**53-1:raise ValueError('User IDs must be safe positive integers')
    if not isinstance(state['message'],str) or not 1<=len(state['message'].strip())<=200:raise ValueError('Message must contain 1–200 Unicode code points after trim')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['probe','start','resume','status','retry','replay','cancel','report'])
    parser.add_argument('--state',type=Path,default=Path('chat-request.json'))
    parser.add_argument('--sender',type=int);parser.add_argument('--recipient',type=int);parser.add_argument('--message')
    parser.add_argument('--fixture',action='store_true',help='Authenticated no-paid protocol fixture')
    parser.add_argument('--prepared-context',type=Path);parser.add_argument('--model');parser.add_argument('--recipient-context',type=Path);parser.add_argument('--after',type=int,default=0)
    args=parser.parse_args(argv);base=os.environ.get('AI_CHAT_URL','').rstrip('/');token=os.environ.get('AI_SERVICE_TOKEN','');url=urlsplit(base)
    if not token or url.scheme not in ('http','https') or not url.netloc or url.path or url.query or url.fragment or url.username:
        parser.error('Set AI_CHAT_URL to the server origin and AI_SERVICE_TOKEN to the API token')
    report=lambda event,data:print(json.dumps({'event':event,'data':data},ensure_ascii=False),flush=True)
    try:
        with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+token},timeout=httpx.Timeout(30,connect=10),follow_redirects=False) as http:
            client=BackendClient(http,lambda state:save_state(args.state,state),report)
            if args.command=='probe':report('health' if args.fixture else 'ready',client.json('GET','/healthz' if args.fixture else '/readyz'));return 0
            if args.command=='start':
                state={'baseUrl':base,'conversationId':str(uuid.uuid4()),'turnId':str(uuid.uuid4()),'senderUserId':args.sender,'recipientUserId':args.recipient,'message':args.message.strip() if args.message else ''}
                if args.fixture:
                    if args.model:parser.error('Fixtures do not accept a paid model')
                    state['protocolFixture']=True
                if args.model:state['model']=args.model.strip()
                if args.prepared_context:state['preparedContext']=json.loads(args.prepared_context.read_text())
                if args.recipient_context:state['recipientContext']=json.loads(args.recipient_context.read_text())
                validate_state(state,base);save_state(args.state,state,create=True)
            else:state=json.loads(args.state.read_text());validate_state(state,base)
            if args.command=='replay':
                if args.after<0:raise ValueError('after must be nonnegative')
                client.replay(state,args.after);return 0
            if args.command=='report':report('report',client.json('GET',root_path(state)+'/turns/'+state['turnId']+'/report'));return 0
            result=client.cancel(state) if args.command=='cancel' else client.status(state) if args.command=='status' else client.retry(state) if args.command=='retry' else client.resume(state)
            return 2 if result['status']=='FAILED' else 0
    except (httpx.HTTPError,ValueError,TypeError,KeyError,OSError,RecoveryNeeded) as exc:
        print(json.dumps({'error':str(exc),'stateFile':str(args.state),'recovery':'Inspect the saved request; use resume or cancel without changing its IDs.'},ensure_ascii=False),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
