import copy
import json
import os
import uuid

import httpx
import pytest

from app import integration_client as integration


def request_state():
    return dict(baseUrl='https://test', conversationId=str(uuid.uuid4()),
                turnId=str(uuid.uuid4()), senderUserId=7, recipientUserId=42,
                message='선물 추천')


def turn(state, status='PROCESSING', cursor=0, text=''):
    data=dict(turnId=state['turnId'], message=state['message'], status=status,
              blocks=[{'type':'text','text':text}] if text else [], lastEventId=cursor)
    if status=='FAILED':data['error']={'message':'공급자 오류'}
    return data


def frame(ident, event, data):
    return f'id: {ident}\r\nevent: {event}\r\ndata: {json.dumps(data, ensure_ascii=False)}\r\n\r\n'


class SplitStream(httpx.SyncByteStream):
    def __init__(self, text):self.raw=text.encode()
    def __iter__(self):
        # Split every UTF-8 character and CRLF boundary deliberately.
        for byte in self.raw:yield bytes([byte])


def test_lost_admission_ack_recovers_same_saved_turn_without_second_creation():
    state=request_state();created=[]
    def server(request):
        if request.url.path.endswith(state['conversationId']):
            return httpx.Response(200,json={'data':{'turns':created}})
        created.append(turn(state,'COMPLETED',1,'완료'))
        raise httpx.ReadError('ack lost',request=request)
    with httpx.Client(base_url=state['baseUrl'],transport=httpx.MockTransport(server)) as http:
        client=integration.BackendClient(http)
        with pytest.raises(httpx.ReadError):client.resume(state)
        assert client.resume(state)['status']=='COMPLETED'
    assert len(created)==1


def test_partial_eof_then_full_snapshot_replaces_text_without_duplicate_generation():
    state=request_state();saved=[];snapshots=[turn(state),turn(state,'COMPLETED',3,'안녕하세요')];streams=[]
    def server(request):
        if request.method=='PUT':return httpx.Response(200,json={'data':{'turns':[snapshots.pop(0)]}})
        streams.append(str(request.url))
        text=': heartbeat\r\n\r\n'+frame(1,'answer.delta',{'delta':'안녕'})
        text+=frame(2,'answer.delta',{'delta':'하세요'}).rstrip('\r\n')
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=SplitStream(text))
    with httpx.Client(base_url=state['baseUrl'],transport=httpx.MockTransport(server)) as http:
        client=integration.BackendClient(http,lambda s:saved.append(copy.deepcopy(s)))
        with pytest.raises(integration.RecoveryNeeded,match='before turn.finished'):client.resume(state)
        assert saved[-1]['turn']['lastEventId']==1
        assert saved[-1]['turn']['blocks'][0]['text']=='안녕'
        assert client.resume(state)['blocks'][0]['text']=='안녕하세요'
    assert len(streams)==1 and 'afterEventId=0' in streams[0]


@pytest.mark.parametrize('gap',[False,True])
def test_duplicate_events_are_ignored_and_gaps_never_advance_cursor(gap):
    state=request_state();initial=turn(state,cursor=1,text='안녕')
    def server(request):
        if request.method=='PUT':return httpx.Response(200,json={'data':{'turns':[initial]}})
        assert request.url.params['afterEventId']=='1'
        text=frame(1,'answer.delta',{'delta':'중복'})
        text+=frame(3 if gap else 2,'answer.delta',{'delta':'하세요'})
        if not gap:text+=frame(3,'turn.finished',turn(state,'COMPLETED',3,'안녕하세요'))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=SplitStream(text))
    with httpx.Client(base_url=state['baseUrl'],transport=httpx.MockTransport(server)) as http:
        client=integration.BackendClient(http)
        if gap:
            with pytest.raises(integration.RecoveryNeeded,match='ID gap'):client.resume(state)
            assert state['turn']==initial
        else:assert client.resume(state)['blocks'][0]['text']=='안녕하세요'


def test_cancellation_of_unknown_turn_never_creates_anything():
    state=request_state();calls=[]
    def server(request):
        calls.append(request);return httpx.Response(404,json={'message':'not found'})
    with httpx.Client(base_url=state['baseUrl'],transport=httpx.MockTransport(server)) as http:
        with pytest.raises(httpx.HTTPStatusError):integration.BackendClient(http).cancel(state)
    assert len(calls)==1 and calls[0].url.path.endswith('/cancellation')


def test_failed_terminal_is_a_nonzero_cli_result_and_journal_stays_private(tmp_path,monkeypatch):
    state=request_state();path=tmp_path/'request.json';integration.save_state(path,state,create=True)
    with pytest.raises(FileExistsError):integration.save_state(path,state,create=True)
    original=httpx.Client
    def server(request):return httpx.Response(200,json={'data':{'turns':[turn(state,'FAILED',1)]}})
    monkeypatch.setattr(integration.httpx,'Client',lambda **kw:original(**kw,transport=httpx.MockTransport(server)))
    monkeypatch.setenv('AI_CHAT_URL',state['baseUrl']);monkeypatch.setenv('AI_SERVICE_TOKEN','private-test-token')
    assert integration.main(['resume','--state',str(path)])==2
    assert os.stat(path).st_mode & 0o777==0o600
    assert 'private-test-token' not in path.read_text()
    assert json.loads(path.read_text())['turn']['status']=='FAILED'


def test_invalid_terminal_cursor_cannot_be_saved():
    state=request_state()
    def server(request):
        if request.method=='PUT':return httpx.Response(200,json={'data':{'turns':[turn(state)]}})
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=frame(1,'turn.finished',turn(state,'COMPLETED',99)))
    with httpx.Client(base_url=state['baseUrl'],transport=httpx.MockTransport(server)) as http:
        with pytest.raises(integration.RecoveryNeeded,match='terminal snapshot'):integration.BackendClient(http).resume(state)
    assert state['turn']['lastEventId']==0
