"""Typed bounds, rather than semantic keyword routing."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

class ClarificationNeeded(ValueError):pass

class Filters(Strict):
    recipient_scope:Literal['current','other']='current'
    brands:list[str]=Field(default_factory=list,max_length=20)
    exclude_brands:list[str]=Field(default_factory=list,max_length=20)
    budget_action:Literal['preserve','replace','clear']='preserve'
    min_price:int|None=Field(default=None,ge=0,le=1_000_000_000)
    max_price:int|None=Field(default=None,ge=0,le=1_000_000_000)
    category_ids:list[str]=Field(default_factory=list,max_length=20,description='Hard category restriction only when the user explicitly restricts the recipient gift to that category. Normally empty: search queries express the product need. Never guess a category from sender interests, occasion or a search query. Replace obsolete categories when the user changes the kind.')
    exclude_category_ids:list[str]=Field(default_factory=list,max_length=20)
    exclude_product_ids:list[str]=Field(default_factory=list,max_length=30)
    product_type:Literal['Shipping','Voucher','Pickup']|None=None

    @model_validator(mode='after')
    def consistent(self):
        if self.min_price is not None and self.max_price is not None and self.min_price>self.max_price:
            raise ValueError('contradictory_budget')
        return self

class ResolvedFilters(Filters):
    # Server-expanded taxonomy IDs use the search API's bounds. Model input stays <=20.
    category_ids:list[str]=Field(default_factory=list,max_length=100)
    exclude_category_ids:list[str]=Field(default_factory=list,max_length=100)

class ProfileTag(Strict):
    axis:Literal['브랜드','IP','카테고리']
    tag:str=Field(min_length=1,max_length=100)

class Profile(Strict):
    recipient_id:str=Field(default='none',min_length=1,max_length=80)
    label:str=Field(default='정보 없음',max_length=80)
    preferences:str=Field(default='',max_length=1200)
    disliked_category_ids:list[str]=Field(default_factory=list,max_length=20)
    disliked_tags:list[ProfileTag]=Field(default_factory=list,max_length=60)
    candidate_ids:list[str]=Field(default_factory=list,max_length=30)
    candidate_scope:Literal['only','preferred']='only'

class ContextArgs(Strict):
    queries:list[str]=Field(min_length=1,max_length=2)
    filters:Filters
    expand_catalog:bool

    @model_validator(mode='after')
    def query_lengths(self):
        if any(not q.strip() or len(q)>200 for q in self.queries):raise ValueError('invalid_query')
        return self

class Evidence(Strict):
    field:Literal['name','brand','description','category']
    quote:str=Field(min_length=1,max_length=300)

class Pick(Strict):
    product_id:str=Field(min_length=1,max_length=100)
    # Bound the shared/server regex. Local generation drops this pattern and
    # retains maxLength; rejected numeric prose receives bounded tool feedback.
    reason:str=Field(min_length=1,max_length=180,pattern=r'^[^\d]{1,180}$',description='One complete Korean sentence about source-confirmed use or a physical feature. Omit ALL brand and product names, prices, numbers, dimensions, quantities and model numbers; those facts are already displayed on the card. Never invent units or conversions.')

    @field_validator('reason')
    @classmethod
    def meaningful_reason(cls,value):
        if not value.strip():raise ValueError('blank_reason')
        return value.strip()

class ShowArgs(Strict):
    message:str=Field(min_length=1,max_length=700)
    filters:Filters
    products:list[Pick]=Field(max_length=2)

class FinalShowArgs(Strict):
    """Search already fixed every condition; selection cannot rewrite them."""
    message:str=Field(min_length=1,max_length=700)
    products:list[Pick]=Field(max_length=2)

class SessionInput(Strict):
    profile:Profile=Field(default_factory=Profile)
    filters:Filters=Field(default_factory=Filters)

class TurnInput(Strict):
    message:str=Field(min_length=1,max_length=1000)
    intent_hint:Literal['auto','gift']='auto'
    model:Literal['openai/gpt-6-luna','google/gemini-3.5-flash-lite','upstage/solar-mini4','google/gemini-3.5-flash']='openai/gpt-6-luna'
    request_id:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')

class ReportInput(Strict):
    report_id:str=Field(pattern=r'^[0-9a-f]{32}$')
    turn_id:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    request_id:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    category:Literal['other','quality','constraints','latency','error']='other'
    message:str=Field(min_length=3,max_length=1500)

def strict_schema(model):
    """Portable inline schema; strict objects include every nullable property."""
    schema=model.model_json_schema()
    definitions=schema.get('$defs',{})
    def inline(node):
        if isinstance(node,list):return [inline(v) for v in node]
        if isinstance(node,dict):
            if '$ref' in node:
                name=node['$ref'].removeprefix('#/$defs/')
                return inline({**definitions[name],**{k:v for k,v in node.items() if k!='$ref'}})
            return {k:inline(v) for k,v in node.items() if k!='$defs'}
        return node
    schema=inline(schema)
    def walk(node):
        if isinstance(node,dict):
            if node.get('type')=='object':
                node['required']=list(node.get('properties',{}))
                node['additionalProperties']=False
            node.pop('default',None)
            for value in node.values():walk(value)
        elif isinstance(node,list):
            for value in node:walk(value)
    walk(schema)
    return schema

def tool(name,model,description):
    return {'type':'function','function':{'name':name,'description':description,'strict':True,'parameters':strict_schema(model)}}

def validate_tool(model,payload,schema=None):
    # Enforce the same required nested fields locally. Provider-side strict
    # mode alone must not let omitted arrays silently clear current filters.
    def verify(node,value):
        if isinstance(node,dict):
            if 'enum' in node and value not in node['enum']:raise ValueError('out_of_scope_value')
            if node.get('type')=='object':
                if not isinstance(value,dict) or set(value)!=set(node.get('properties',{})):raise ValueError('incomplete_tool_object')
                for k,v in value.items():verify(node['properties'][k],v)
            elif node.get('type')=='array':
                if not isinstance(value,list) or len(value)>node.get('maxItems',1000):raise ValueError('tool_array_limit')
                for v in value:verify(node.get('items',{}),v)
    verify(schema or strict_schema(model),payload)
    f=payload.get('filters',{})
    if type(f.get('min_price')) is int and type(f.get('max_price')) is int and f['min_price']>f['max_price']:
        raise ClarificationNeeded(f"최소 {f['min_price']:,}원과 최대 {f['max_price']:,}원은 함께 만족할 수 없어요. 어느 가격 조건을 바꿀까요?")
    return model.model_validate(payload)

def scoped_tool(template,categories,observed_ids=None,budget=None):
    """Ground IDs in data; terminal context price limits cannot be relaxed."""
    import copy
    t=copy.deepcopy(template);props=t['function']['parameters']['properties']
    f=props.get('filters',{}).get('properties',{})
    for name in ['category_ids','exclude_category_ids']:
        if name in f:
            if categories:f[name]['items']['enum']=sorted(categories)
            else:f[name]['maxItems']=0
    if budget is not None:
        f['budget_action']['enum']=['preserve']
        for name in ['min_price','max_price']:f[name]['enum']=list(dict.fromkeys([getattr(budget,name),None]))
    if observed_ids is not None:
        if observed_ids:props['products']['items']['properties']['product_id']['enum']=sorted(observed_ids)
        else:props['products']['maxItems']=0
    return t

CONTEXT_TOOL=tool('gift_context',ContextArgs,'Search/hydrate actual gift candidates for the requested kind and use. Follow the cached recipient need; occasion stereotypes do not override it. If an essential physical fit/connector/device specification is missing, ask one question in TEXT before using this tool; do not search a different connection or use as a workaround. Never use for casual chat. At most one call, up to two queries. Respect saved-pool scope.')
SHOW_TOOL=tool('show_gifts',ShowArgs,'ONLY for new or changed product recommendations. For existing-product name/price questions answer in TEXT and do not call this tool. Pick one or two observed IDs that meet the recipient need and requested use; do not fill slots with unrelated alternatives. Short intro and reasons grounded in actual product facts. The server supplies source evidence from the catalog; do not copy quotes. Empty products means no suitable recommendation, never a factual answer. Do not infer compatibility or stock.')
FINAL_SHOW_TOOL=tool('show_gifts',FinalShowArgs,'Finish this recommendation from the observed candidates. All filters are already fixed by gift_context and enforced by the server; do not return or change filters. Pick one or two candidates that meet the recipient need and requested use. Do not fill slots with unrelated alternatives. Short intro and one-sentence reasons grounded in actual facts; source evidence is supplied by the server. Empty products means no suitable recommendation. Do not infer compatibility or current stock.')
