"""Free HF API trial. Python 3.10+, httpx; DEMO_ACCESS_TOKEN stays in private environment."""
import json,os,sys,uuid
from urllib.parse import urlsplit
import httpx

def main():
    base=os.environ.get('AI_CHAT_URL','https://wjo-ai-chat-demo.hf.space').rstrip('/')
    parsed=urlsplit(base);code=os.environ.get('DEMO_ACCESS_TOKEN','')
    if parsed.scheme!='https' or parsed.hostname!='wjo-ai-chat-demo.hf.space' or parsed.port not in (None,443) or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Use the documented HTTPS HF origin; credentials are not sent to redirects or other origins')
    if not code:raise ValueError('Set DEMO_ACCESS_TOKEN privately before running')
    with httpx.Client(base_url=base,timeout=httpx.Timeout(30,connect=10),follow_redirects=False) as client:
        def request(method,path,**kwargs):
            response=client.request(method,path,**kwargs);response.raise_for_status();return response.json()
        session=request('POST','/demo/session',json={'token':code})
        client.headers['Authorization']='Bearer '+code
        cid,tid=str(uuid.uuid4()),str(uuid.uuid4());root='/demo/api/test/conversations/'+cid
        request('PUT',root,json={'senderUserId':session['senderUserId'],'recipientUserId':900000902})
        request('PUT',root+'/turns/'+tid,json={'message':'/mock text'})
        with client.stream('GET',root+'/turns/'+tid+'/events',params={'afterEventId':0},headers={'Accept':'text/event-stream'}) as response:
            response.raise_for_status()
            for line in response.iter_lines():print(line)
        turn=request('GET',root+'/turns/'+tid)['data']
        print(json.dumps({'conversationId':cid,'turnId':tid,'status':turn['status']},ensure_ascii=False))
        if turn['status'] not in {'COMPLETED','FAILED','CANCELLED'}:raise RuntimeError('Stream ended before terminal; query the same IDs and resume from lastEventId')
        return 2 if turn['status']=='FAILED' else 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        # Do not print request objects, headers, cookies or credential values.
        print('API trial failed: '+type(exc).__name__+'; inspect the saved IDs and retry without changing the request.',file=sys.stderr)
        raise SystemExit(1)
