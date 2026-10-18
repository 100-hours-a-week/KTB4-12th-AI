"""Typed HF search adapter. Raw recipient data never enters responder messages."""
import asyncio,hashlib,json
from app.gift_core.contracts import Filters,ResolvedFilters,Profile
from app.gift_core.harness import Harness
from app.search_models import Filters as SearchFilters,SearchRequest
from app.public_answer import public_products
from app.diagnostics import operation,sync_operation

def view(raw):
 facts=public_products([raw])[0]
 ident=str(facts['productId']);category=raw.get('categoryId')
 if type(category) is not int or not 1<=category<=2**53-1:raise ValueError('Invalid catalog category identity')
 availability=raw.get('availability','UNKNOWN')
 return {'product_id':ident,'name':facts.get('name',''),'brand':facts.get('brand') or '',
         'price':facts.get('price'),'category':facts.get('category') or '',
         'category_id':str(category),'description':facts.get('description') or '',
         'product_type':raw.get('productType'),'sold_out':availability in ('UNAVAILABLE','unavailable'),
         'category_status':'ASSIGNED','image':facts.get('image'),'availability':availability,
         'description_origin':raw.get('descriptionOrigin')}

def public_view(row):
 return {k:row.get(k) for k in ('product_id','name','brand','price','category','category_id','description','product_type','sold_out','category_status')}

def hf_facts(rows):
 return public_products([{'productId':int(p['product_id']),'name':p['name'],'brand':p['brand'],'price':p['price'],'category':p['category'],'description':p['description'],'image':p.get('image')} for p in rows])

class CatalogAdapter:
 def __init__(self,search,meta,receipt):
  self.search_api=search;self.snapshot=meta['snapshotId'];self.products={};self.receipt=receipt
  self.brand_names=[b['name'] for b in meta.get('brands',[]) if isinstance(b,dict) and isinstance(b.get('name'),str)]
  self.categories=[{'category_id':str(c['id']),'category':c['name'],'category_group':c['group'],'parent_id':str(c['parentId'])} for c in meta['categories']]
  self.parents={};self.category_ids=set()
  for c in self.categories:
   self.category_ids.update([c['category_id'],c['parent_id']]);self.parents.setdefault(c['parent_id'],[]).append(c['category_id'])
 def validate_filters(self,f):
  if set(f.category_ids+f.exclude_category_ids)-self.category_ids:raise ValueError('unknown_category')
 def expand(self,ids):
  return list(dict.fromkeys(child for ident in ids for child in self.parents.get(ident,[ident])))
 def resolve_filters(self,patch,prior,profile):
  self.validate_filters(patch);f=(self.public_filters if patch is prior and hasattr(self,'public_filters') else patch).model_copy(deep=True)
  if f.budget_action=='preserve':f.min_price,f.max_price=(prior.min_price,prior.max_price) if f.recipient_scope==prior.recipient_scope else (None,None)
  elif f.budget_action=='clear':f.min_price,f.max_price=None,None
  self.public_filters=f.model_copy(deep=True)
  if f.recipient_scope=='current' and set(profile.disliked_category_ids)-self.category_ids:raise ValueError('Unknown recipient category mapping')
  f.category_ids=self.expand(f.category_ids);f.exclude_category_ids=self.expand(f.exclude_category_ids)
  f.exclude_category_ids=self.expand(list(dict.fromkeys(f.exclude_category_ids+(profile.disliked_category_ids if f.recipient_scope=='current' else []))))
  return ResolvedFilters.model_validate(f.model_dump())
 def allowed(self,p,f):
  return ((not f.brands or p['brand'] in f.brands) and p['brand'] not in f.exclude_brands and not p['sold_out'] and type(p['price']) is int and p['price']>=0 and (f.min_price is None or p['price']>=f.min_price) and (f.max_price is None or p['price']<=f.max_price) and (not f.category_ids or p['category_id'] in f.category_ids) and p['category_id'] not in f.exclude_category_ids and p['product_id'] not in f.exclude_product_ids and (f.product_type is None or p['product_type']==f.product_type))
 async def hydrate(self,ids):
  if not ids:return [],[]
  numeric=[int(x) for x in ids]
  if any(str(n)!=x or not 1<=n<=2**53-1 for n,x in zip(numeric,ids)):raise ValueError('Invalid prepared product identity')
  result=await operation(self.receipt,'catalog.get_products',{'productIds':numeric,'snapshotId':self.snapshot},lambda:self.search_api.get_products(numeric,snapshot_id=self.snapshot))
  if result.get('snapshotId')!=self.snapshot:raise ValueError('Prepared snapshot changed')
  raw=result.get('products',result.get('hits',[]));rows=[view(p) for p in raw]
  if len({p['product_id'] for p in rows})!=len(rows) or set(p['product_id'] for p in rows)-set(ids):raise ValueError('Prepared response identity mismatch')
  self.products.update({p['product_id']:p for p in rows});return rows,[x for x in ids if x not in self.products]
 async def search(self,query,f,*,include_ids=()):
  self.validate_filters(f);wire=SearchFilters(brands=f.brands,excludeBrands=f.exclude_brands,minPrice=f.min_price,maxPrice=f.max_price,categoryIds=[int(x) for x in f.category_ids],excludeCategoryIds=[int(x) for x in f.exclude_category_ids],excludeProductIds=[int(x) for x in f.exclude_product_ids],productTypes=[f.product_type] if f.product_type else [],availability='available_or_unknown')
  wire.include_product_ids=[int(ident) for ident in include_ids]
  request=SearchRequest(query=query,filters=wire,limit=30 if include_ids else 15).model_dump(by_alias=True)
  result=await operation(self.receipt,'catalog.search',{'request':request,'snapshotId':self.snapshot},lambda:self.search_api.search(request,snapshot_id=self.snapshot))
  if result.get('snapshotId')!=self.snapshot:raise ValueError('Search snapshot changed')
  rows=[view(p) for p in result['hits']]
  if include_ids and any(p['product_id'] not in include_ids for p in rows):raise ValueError('Prepared search scope invariant')
  if any(not self.allowed(p,f) for p in rows):raise ValueError('Search filter invariant')
  self.products.update({p['product_id']:p for p in rows});return rows

class ContextAdapter:
 def __init__(self,catalog):self.catalog=catalog
 async def load(self,owner,profile,filters,queries=(),expand=False,raw_pool=False):
  if filters.recipient_scope=='current' and set(profile.disliked_category_ids)-self.catalog.category_ids:raise ValueError('Unknown recipient category mapping')
  if profile.candidate_ids and not expand:
   rows,unknown=await self.catalog.hydrate(profile.candidate_ids)
   if unknown:raise ValueError('Unknown prepared product ID')
   rows=rows if raw_pool else [p for p in rows if self.catalog.allowed(p,filters)];source='saved_pool'
  else:
   if profile.candidate_ids and profile.candidate_scope=='only' and expand:raise ValueError('prepared_scope_forbids_expansion')
   # Private profile text is an independent retrieval input, never responder input.
   effective=list(dict.fromkeys(([profile.preferences.strip()] if filters.recipient_scope=='current' and profile.preferences.strip() else [])+list(queries)))[:2]
   if not effective:return {'products':[],'source':'no_pool','snapshot':self.catalog.snapshot},False
   self.catalog.receipt['searchBranches']=len(effective)
   branches=await asyncio.gather(*(self.catalog.search(q,filters) for q in effective));rows=[];seen=set()
   for rank in range(max(map(len,branches),default=0)):
    for branch in branches:
     if rank<len(branch) and branch[rank]['product_id'] not in seen:rows.append(branch[rank]);seen.add(branch[rank]['product_id'])
   rows=rows[:30];source='catalog_search'
  self.catalog.receipt['observedProducts']=hf_facts(rows)
  # Private exclusions and derived filters also stay outside public tool results.
  return {'products':[public_view(p) for p in rows],'source':source,'snapshot':self.catalog.snapshot,'notice':'가격·정보는 snapshot 기준이며 현재 판매 상태는 확인하지 않았습니다.'},False

class PublicHarness(Harness):
 def validate_show(self,args,s,observed,prior=None):
  validate=super().validate_show
  return sync_operation(self.catalog.receipt,'show_gifts',args.model_dump(),lambda:validate(args,s,observed,prior),lambda result:{'products':result[0],'filters':result[1].model_dump()})
 def state(self,s,request,context):
  # Filter only user-origin constraints. Private recipient exclusions stay server-side.
  f=s.filters.model_dump();f['exclude_category_ids']=list(getattr(s,'public_exclusions',[]))
  return {'intent_hint':request.intent_hint,'recipient_label':'현재 대화의 수신자','candidate_count':len(s.profile.candidate_ids),'candidate_scope':s.profile.candidate_scope,'filters':f,'last_products':self.compact(s.last_products),'gift_context':context,'categories':self.catalog.categories}
