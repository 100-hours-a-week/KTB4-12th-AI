import hashlib
import json
from pathlib import Path
import numpy as np
import pytest
from pydantic import ValidationError
from search import SearchService, SearchRequest


class FakeEmbedding:
    def __init__(self): self.calls=0
    async def get(self,query,source):
        self.calls+=1
        v=np.zeros(768,dtype=np.float32);v[0]=1
        return v,False


@pytest.fixture
def service(tmp_path):
    products=[]
    for i,(name,brand,price,cat,available) in enumerate([
        ('스탠리 텀블러 500ml','스탠리',30000,'cup','unknown'),
        ('보온 머그컵','브랜드B',50000,'cup','available'),
        ('스탠리 텀블러 대형','스탠리',80000,'cup','available'),
        ('무선 스피커','브랜드C',45000,'audio','unavailable'),
        ('머그컵 2개 세트','브랜드B',29999,'cup','available'),
    ]):
        products.append({'id':f'source:{i}','backend_product_id':None,'name':name,'brand':brand,'category_id':cat,
            'category':cat,'category_group':'생활','kind':name.split()[0],'product_type':'Shipping',
            'price':price,'description':'스테인리스 소재' if i==0 else '상품 설명','attributes':{'소재':'스테인리스'} if i==0 else {},
            'tags':[],'availability':available,'product_url':'https://example.com','image':'/image.avif','image_large':'/large.avif','image_fallback':'/image.webp',
            'document':name,'description_origin':'source'})
    catalog={'products':products,'taxonomy':{'categories':[{'category_id':c,'category':c,'category_group':'생활'} for c in ['cup','audio']]},'source':'test','availability_note':'test'}
    raw=json.dumps(catalog,ensure_ascii=False).encode();(tmp_path/'catalog.json').write_bytes(raw)
    vectors=np.zeros((5,768),dtype=np.float32);vectors[:,0]=[1,.8,.9,.7,.6];vectors[:,1]=[.1,.2,.3,.4,.5]
    b=vectors.tobytes();(tmp_path/'vectors.f32').write_bytes(b)
    manifest={'catalog_sha256':hashlib.sha256(raw).hexdigest(),'vectors_sha256':hashlib.sha256(b).hexdigest(),
              'product_ids':[p['id'] for p in products],'token_counts':[10]*5,'max_tokens':1024,'model':'test'}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    service=SearchService(tmp_path,FakeEmbedding())
    yield service
    service.close()


@pytest.mark.parametrize('mode',['hybrid','lexical','dense'])
async def test_same_hard_filters_for_every_ranker(service,mode):
    r=await service.search({'query':'텀블러','mode':mode,'filters':{'minPrice':30000,'maxPrice':50000,'categoryIds':['cup'],'excludeProductIds':['source:1']}})
    assert [p['id'] for p in r['hits']]==['source:0']
    assert r['eligibleCount']==1


async def test_unknown_availability_never_promoted_to_available(service):
    r=await service.search({'query':'','filters':{'availability':'available'}})
    assert {p['id'] for p in r['hits']}=={'source:1','source:2','source:4'}
    r=await service.search({'query':'','filters':{'availability':'unknown'}})
    assert [p['id'] for p in r['hits']]==['source:0']


async def test_exclusion_wins_and_does_not_embed_empty_catalog(service):
    r=await service.search({'query':'컵','filters':{'categoryIds':['cup'],'excludeCategoryIds':['cup']}})
    assert r['hits']==[] and r['status']=='NO_MATCH'
    assert service.embedding.calls==0


async def test_schema_and_unknown_filter_errors_are_not_silently_ignored(service):
    for payload in [{'filters':{'minPrice':50000,'maxPrice':30000}},{'limit':0},{'filters':{'minPrice':'30000'}},{'filters':{'material':'steel'}}]:
        with pytest.raises(ValidationError):await service.search(payload)
    with pytest.raises(ValueError):await service.search({'filters':{'categoryIds':['not-real']}})
    with pytest.raises(ValueError):await service.search({'filters':{'brands':['not-real']}})


async def test_attributes_search_and_brand_exact_filter(service):
    r=await service.search({'query':'스테인리스','mode':'lexical','filters':{'brands':['스탠리']}})
    assert r['hits'][0]['id']=='source:0'
    assert '설명·속성' in r['hits'][0]['matchedFields']
    r=await service.search({'query':'텀블러','filters':{'excludeBrands':['스탠리']}})
    assert all(p['brand']!='스탠리' for p in r['hits'])


async def test_soft_preferences_do_not_remove_candidates(service):
    base=await service.search({'query':'선물'})
    adjusted=await service.search({'query':'선물','preferences':{'downrankCategoryIds':['cup']}})
    assert {p['id'] for p in base['hits']}=={p['id'] for p in adjusted['hits']}
    assert adjusted['hits'][0]['categoryId']=='audio'


async def test_internal_calls_cache_and_snapshot_are_stable(service):
    req=SearchRequest(query='선물')
    a=await service.search(req,source='chat')
    b=await service.search(req,source='profile')
    assert a['hits']==b['hits'] and b['timing']['cached']
    assert service.embedding.calls==1
    with pytest.raises(ValueError):await service.search(req,snapshot='stale')
    with pytest.raises(ValueError):service.get_products(['source:0'],snapshot='stale')


def test_id_lookup_keeps_unavailable_product_and_reports_missing(service):
    r=service.get_products(['source:3','nonexistent'])
    assert r['products'][0]['availability']=='unavailable'
    assert r['missingIds']==['nonexistent']
    assert r['products'][0]['sourceText']=='무선 스피커'


async def test_pagination_is_stable_without_duplicates(service):
    a=await service.search({'limit':2,'offset':0})
    b=await service.search({'limit':2,'offset':2})
    assert not {p['id'] for p in a['hits']} & {p['id'] for p in b['hits']}
    assert a['candidateCount']==5 and b['hasMore']


def test_corrupt_vectors_are_rejected(service,tmp_path):
    with (tmp_path/'vectors.f32').open('r+b') as f:f.write(b'xxxx')
    with pytest.raises(ValueError,match='hash_mismatch'):SearchService(tmp_path)
