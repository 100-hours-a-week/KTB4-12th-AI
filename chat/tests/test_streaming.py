"""Provider timing, durable writes, cancellation and incomplete stream behavior."""
import asyncio,json,time
import httpx,pytest
from app.streaming import stream_answer

class Stream(httpx.AsyncByteStream):
 def __init__(self,steps):self.steps=steps;self.closed=False
 async def __aiter__(self):
  for delay,value in self.steps:
   await asyncio.sleep(delay)
   if isinstance(value,Exception):raise value
   yield ('data: '+json.dumps(value)+'\n\n').encode()
 async def aclose(self):self.closed=True

def delta(text):return {'choices':[{'delta':{'content':text}}]}
def end(reason='stop'):return {'choices':[{'delta':{},'finish_reason':reason}]}
class Runtime:
 def __init__(self,stream,delay=0):
  self.http=httpx.AsyncClient(base_url='https://test',transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=stream)))
  self.events=[];self.delay=delay;self.first=asyncio.Event()
 async def emit(self,cid,tid,kind,payload):
  await asyncio.sleep(self.delay)
  self.events.append((time.monotonic(),payload['delta']));self.first.set()

def receipt():return {'usage':[],'startedMonotonic':time.monotonic()}

async def test_first_text_commits_immediately_and_small_buffer_flushes_during_provider_silence():
 stream=Stream([(0,delta('첫')), (.02,delta(' 번째')), (.3,delta(' 답변')), (0,end())]);r=Runtime(stream);trace=receipt()
 try:await stream_answer(r,'c','t',trace,{})
 finally:await r.http.aclose()
 assert ''.join(d for _,d in r.events)=='첫 번째 답변'
 times=[t-trace['startedMonotonic'] for t,_ in r.events]
 assert times[0]<.06 and .07<times[1]<.22 and times[2]>=.3
 assert trace['streaming']['answerEvents']==3
 assert trace['streaming']['firstCommittedDeltaMs']>=trace['streaming']['firstProviderDeltaMs']
 assert stream.closed

async def test_provider_keeps_reading_during_slow_durable_write():
 stream=Stream([(0,delta('a')),(.01,delta('b')),(.01,delta('c')),(0,end())]);r=Runtime(stream,delay=.1);trace=receipt()
 try:await stream_answer(r,'c','t',trace,{})
 finally:await r.http.aclose()
 assert ''.join(d for _,d in r.events)=='abc'
 assert trace['streaming']['maxProviderGapMs']<60
 assert trace['streaming']['storeMs']>=190
 assert trace['streaming']['firstCommittedDeltaMs']>=90

@pytest.mark.parametrize('ending',[end('length'),{'error':{'message':'provider failure'}}])
async def test_incomplete_or_error_preserves_partial_but_never_reports_success(ending):
 stream=Stream([(0,delta('일부')), (0,delta(' 답변')), (0,ending)]);r=Runtime(stream)
 try:
  with pytest.raises(ValueError):await stream_answer(r,'c','t',receipt(),{})
  assert ''.join(d for _,d in r.events)=='일부 답변'
 finally:await r.http.aclose()
 assert stream.closed

async def test_cancel_stops_provider_and_no_extra_write():
 stream=Stream([(0,delta('일부')), (10,delta('이어지는 내용'))]);r=Runtime(stream)
 task=asyncio.create_task(stream_answer(r,'c','t',receipt(),{}))
 await r.first.wait();task.cancel()
 with pytest.raises(asyncio.CancelledError):await task
 assert len(r.events)==1 and stream.closed
 await r.http.aclose()

async def test_reasoning_deltas_are_not_visible_and_usage_is_retained():
 stream=Stream([(0,{'choices':[{'delta':{'reasoning':'internal','reasoning_details':[{'text':'internal'}]}}]}),(0,delta('최종 답변')),(0,end()),(0,{'usage':{'completion_tokens':120,'completion_tokens_details':{'reasoning_tokens':100}}})])
 r=Runtime(stream);trace=receipt()
 try:await stream_answer(r,'c','t',trace,{})
 finally:await r.http.aclose()
 assert ''.join(text for _,text in r.events)=='최종 답변'
 assert trace['usage'][0]['completion_tokens_details']['reasoning_tokens']==100
