"""Task-local transport trace; never contains prompts, tools or recipient data."""
from contextvars import ContextVar
TRACE=ContextVar('paid_request_trace',default=None)
def attempted(body,ident):
 trace=TRACE.get()
 if trace is not None:
  trace.append({'index':len(trace)+1,'reservationId':ident,'model':body['model'],'reasoning':body.get('reasoning',{}),'maxTokens':body['max_tokens'],'stream':bool(body.get('stream'))})
