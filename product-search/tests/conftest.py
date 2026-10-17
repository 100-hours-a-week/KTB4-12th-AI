import hashlib
import json
import numpy as np
import pytest
from search import SearchService


class FakeEmbedding:
    def __init__(self): self.calls=0
    async def get(self,query,source):
        self.calls+=1
        v=np.zeros(768,dtype=np.float32);v[0]=1
        return v,False


@pytest.fixture
def service(tmp_path, request):
    product_ids = getattr(request, "param", None) or [1, 2, 3, 4, 5]
    products=[]
    for i,(name,brand,price,cat,available) in enumerate([
        ('스탠리 텀블러 500ml','스탠리',30000,'cup','unknown'),
        ('보온 머그컵','브랜드B',50000,'cup','available'),
        ('스탠리 텀블러 대형','스탠리',80000,'cup','available'),
        ('무선 스피커','브랜드C',45000,'audio','unavailable'),
        ('머그컵 2개 세트','브랜드B',29999,'cup','available'),
    ]):
        category_id, parent_id, group = (11, 1, '생활') if cat == 'cup' else (12, 2, '전자')
        products.append({'id':product_ids[i],'source_product_id':f'source:{i}','name':name,'brand':brand,
            'category_id':category_id,'source_category_id':cat,'parent_category_id':parent_id,
            'category':cat,'category_group':group,'kind':name.split()[0],'product_type':'Shipping',
            'price':price,'description':'스테인리스 소재' if i==0 else '상품 설명','attributes':{'소재':'스테인리스'} if i==0 else {},
            'tags':[],'availability':available,'product_url':'https://example.com','image':'/image.avif','image_large':'/large.avif','image_fallback':'/image.webp',
            'document':name,'description_origin':'source'})
    categories = [
        {'category_id': 1, 'source_category_id': 'group-home', 'parent_id': None, 'category': '생활', 'category_group': '생활'},
        {'category_id': 2, 'source_category_id': 'group-electronics', 'parent_id': None, 'category': '전자', 'category_group': '전자'},
        {'category_id': 11, 'source_category_id': 'cup', 'parent_id': 1, 'category': 'cup', 'category_group': '생활'},
        {'category_id': 12, 'source_category_id': 'audio', 'parent_id': 2, 'category': 'audio', 'category_group': '전자'},
    ]
    catalog={'format':'product-search-catalog/3','products':products,'taxonomy':{'categories':categories},'source':'test','availability_note':'test'}
    raw=json.dumps(catalog,ensure_ascii=False).encode();(tmp_path/'catalog.json').write_bytes(raw)
    vectors=np.zeros((5,768),dtype=np.float32);vectors[:,0]=[1,.8,.9,.7,.6];vectors[:,1]=[.1,.2,.3,.4,.5]
    b=vectors.tobytes();(tmp_path/'vectors.f32').write_bytes(b)
    manifest={'catalog_sha256':hashlib.sha256(raw).hexdigest(),'vectors_sha256':hashlib.sha256(b).hexdigest(),
              'product_ids':[p['id'] for p in products],'token_counts':[10]*5,'max_tokens':1024,'model':'test'}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    service=SearchService(tmp_path,FakeEmbedding())
    yield service
    service.close()
