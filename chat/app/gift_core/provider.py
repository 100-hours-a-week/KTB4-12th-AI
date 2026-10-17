import asyncio,json
class ProviderError(RuntimeError):pass

async def sse_json(response, *, max_chars=250_000):
    """HTTPX handles fragmented UTF-8/lines; SSE dispatches at blank lines."""
    data=[];size=0
    async for line in response.aiter_lines():
        size+=len(line)
        if size>max_chars:raise ProviderError('provider_stream_limit')
        if line=='':
            if data:
                raw='\n'.join(data);data=[]
                if raw=='[DONE]':return
                try:yield json.loads(raw)
                except ValueError:raise ProviderError('invalid_provider_sse') from None
        elif line.startswith('data:'):data.append(line[5:].lstrip(' '))
        # comments / event / id / retry fields do not become answer text.
    if data:
        raw='\n'.join(data)
        if raw!='[DONE]':
            try:yield json.loads(raw)
            except ValueError:raise ProviderError('invalid_provider_sse') from None

class ScriptedProvider:
    """Test fixture. Each model call consumes one explicit event sequence."""
    mode='fixture'
    def __init__(self,scripts,delay=0):self.scripts=list(scripts);self.calls=[];self.delay=delay;self.closed=False
    async def stream(self,messages,tools,model):
        self.calls.append({'messages':messages,'tools':tools,'model':model})
        if not self.scripts:raise ProviderError('fixture_exhausted')
        for event in self.scripts.pop(0):
            await asyncio.sleep(self.delay)
            if isinstance(event,Exception):raise event
            yield event
    async def close(self):self.closed=True

def tool_events(name,args):
    raw=json.dumps(args,ensure_ascii=False)
    return [{'kind':'tool','value':{'index':0,'id':'fixture-call','type':'function','function':{'name':name,'arguments':raw[:len(raw)//2]}}},
            {'kind':'tool','value':{'index':0,'function':{'arguments':raw[len(raw)//2:]}}},
            {'kind':'finish','value':'tool_calls'},{'kind':'usage','value':{'cost':0}}]
