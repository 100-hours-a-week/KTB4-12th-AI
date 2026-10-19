"""Reserve on existing PostgreSQL before any paid request; unknown costs stay reserved."""
import json,math,os,re,uuid
from contextlib import asynccontextmanager
from app.errors import APIError

ACCOUNT='gift-chat-harness-2026-10-06'
CAPS={'prompt':0.6,'completion':5,'request':0}
MODEL_CAPS={'anthropic/claude-haiku-5.5':{'prompt':0.11,'completion':0.55,'request':0},
            'openai/gpt-6-luna':{'prompt':0.11,'completion':0.55,'request':0},
            'google/gemini-3.8-flash':{'prompt':1.6,'completion':10,'request':0},
            'anthropic/claude-sonnet-5.5':{'prompt':3,'completion':15,'request':0}}

def budget_settings(account=None,expected_limit=None):
    account=os.environ.get('BUDGET_ACCOUNT',ACCOUNT) if account is None else account
    if not isinstance(account,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',account):
        raise ValueError('Explicit BUDGET_ACCOUNT required')
    if expected_limit is None:expected_limit=os.environ.get('BUDGET_EXPECTED_LIMIT_USD','5' if account==ACCOUNT else '')
    if isinstance(expected_limit,bool):raise ValueError('Explicit positive BUDGET_EXPECTED_LIMIT_USD required')
    try:expected_limit=float(expected_limit)
    except (TypeError,ValueError):raise ValueError('Explicit positive BUDGET_EXPECTED_LIMIT_USD required') from None
    if not math.isfinite(expected_limit) or expected_limit<=0:raise ValueError('Explicit positive BUDGET_EXPECTED_LIMIT_USD required')
    return account,expected_limit

def validate_limit(result,expected_limit):
    if (not isinstance(result,dict) or type(result.get('limitUsd')) not in (int,float)
            or not math.isfinite(result['limitUsd']) or result['limitUsd']!=expected_limit):
        raise ValueError('Configured cumulative account limit differs from ledger')

def bounded_request(body):
    body=dict(body);cap=dict(MODEL_CAPS.get(body['model'],CAPS))
    provider=dict(body.get('provider',{}));provider['max_price']=cap;body['provider']=provider
    # One byte per token bounds text input conservatively, including templates.
    size=len(json.dumps(body,ensure_ascii=False).encode())
    amount=max(.01,math.ceil(((size+4096)*cap['prompt']/1e6+body['max_tokens']*cap['completion']/1e6)*2*100)/100)
    return body,amount

def safe_cost(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None

class BudgetHTTP:
    def __init__(self,http,store,account=None,*,expected_limit=None,check_summary=False):
        self.http,self.store=http,store
        self.account,self.expected_limit=budget_settings(account,expected_limit)
        self.check_summary=check_summary
    def __getattr__(self,key):return getattr(self.http,key)
    async def reserve(self,body):
        if self.check_summary:
            try:validate_limit(await self.store.call('budget_summary',account=self.account),self.expected_limit)
            except ValueError:raise APIError(503,'설정한 누적 예산 한도와 원장이 일치하지 않습니다.') from None
        body,amount=bounded_request(body);ident=str(uuid.uuid4())
        # If this acknowledgement is lost, do not send the paid request.
        await self.store.call('budget_reserve',account=self.account,ident=ident,amount=amount,model=body['model'])
        return body,ident
    async def finish(self,ident,cost,status):
        try:await self.store.call('budget_finish',account=self.account,ident=ident,cost=safe_cost(cost),status=status)
        except Exception:pass  # Durable reservation remains charged; never refund uncertainty.
    async def post(self,path,**kwargs):
        body,ident=await self.reserve(kwargs['json']);kwargs['json']=body;cost=None;status='unknown'
        try:
            from app.model_trace import attempted
            attempted(body,ident)
            response=await self.http.post(path,**kwargs)
            if response.status_code==200:
                cost=response.json().get('usage',{}).get('cost');status='received'
            return response
        finally:await self.finish(ident,cost,status)
    @asynccontextmanager
    async def stream(self,method,path,**kwargs):
        body,ident=await self.reserve(kwargs['json']);kwargs['json']=body
        tracker={'cost':None,'status':'unknown'}
        try:
            from app.model_trace import attempted
            attempted(body,ident)
            async with self.http.stream(method,path,**kwargs) as response:
                yield BudgetResponse(response,tracker)
        finally:await self.finish(ident,tracker['cost'],tracker['status'])

class BudgetResponse:
    def __init__(self,response,tracker):self.response,self.tracker=response,tracker
    def __getattr__(self,key):return getattr(self.response,key)
    async def aiter_lines(self):
        async for line in self.response.aiter_lines():
            if line.startswith('data:'):
                try:
                    packet=json.loads(line[5:].strip())
                    if isinstance(packet,dict) and isinstance(packet.get('usage'),dict):
                        self.tracker.update(cost=packet['usage'].get('cost'),status='received')
                except ValueError:pass
            yield line
