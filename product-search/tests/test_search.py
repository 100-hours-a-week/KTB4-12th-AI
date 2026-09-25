import pytest
from pydantic import ValidationError
from search import SearchService, SearchRequest


@pytest.mark.parametrize('mode',['hybrid','lexical','dense'])
async def test_same_hard_filters_for_every_ranker(service,mode):
    r=await service.search({'query':'텀블러','mode':mode,'filters':{'minPrice':30000,'maxPrice':50000,'categoryIds':[11],'excludeProductIds':[2]}})
    assert [p['productId'] for p in r['hits']]==[1]
    assert r['eligibleCount']==1


async def test_unknown_availability_never_promoted_to_available(service):
    r=await service.search({'query':'','filters':{'availability':'available'}})
    assert {p['productId'] for p in r['hits']}=={2,3,5}
    r=await service.search({'query':'','filters':{'availability':'unknown'}})
    assert [p['productId'] for p in r['hits']]==[1]


async def test_exclusion_wins_and_does_not_embed_empty_catalog(service):
    r=await service.search({'query':'컵','filters':{'categoryIds':[11],'excludeCategoryIds':[11]}})
    assert r['hits']==[] and r['status']=='NO_MATCH'
    assert service.embedding.calls==0


async def test_schema_and_unknown_filter_errors_are_not_silently_ignored(service):
    for payload in [{'filters':{'minPrice':50000,'maxPrice':30000}},{'limit':0},{'filters':{'minPrice':'30000'}},{'filters':{'material':'steel'}}]:
        with pytest.raises(ValidationError):await service.search(payload)
    with pytest.raises(ValueError):await service.search({'filters':{'categoryIds':[99999]}})
    with pytest.raises(ValueError):await service.search({'filters':{'brands':['not-real']}})


async def test_attributes_search_and_brand_exact_filter(service):
    r=await service.search({'query':'스테인리스','mode':'lexical','filters':{'brands':['스탠리']}})
    assert r['hits'][0]['productId']==1
    assert '설명·속성' in r['hits'][0]['matchedFields']
    r=await service.search({'query':'텀블러','filters':{'excludeBrands':['스탠리']}})
    assert all(p['brand']!='스탠리' for p in r['hits'])


async def test_soft_preferences_do_not_remove_candidates(service):
    base=await service.search({'query':'선물'})
    adjusted=await service.search({'query':'선물','preferences':{'downrankCategoryIds':[11]}})
    assert {p['productId'] for p in base['hits']}=={p['productId'] for p in adjusted['hits']}
    assert adjusted['hits'][0]['categoryId']==12


async def test_internal_calls_cache_and_snapshot_are_stable(service):
    req=SearchRequest(query='선물')
    a=await service.search(req,source='chat')
    b=await service.search(req,source='profile')
    assert a['hits']==b['hits'] and b['timing']['cached']
    assert service.embedding.calls==1
    with pytest.raises(ValueError):await service.search(req,snapshot='stale')
    with pytest.raises(ValueError):service.get_products([1],snapshot='stale')


def test_id_lookup_keeps_unavailable_product_and_reports_missing(service):
    r=service.get_products([4,99999])
    assert r['products'][0]['availability']=='unavailable'
    assert r['missingIds']==[99999]
    assert r['products'][0]['sourceText']=='무선 스피커'
    assert r['products'][0]['productId'] == 4
    assert r['products'][0]['sourceProductId'] == 'source:3'
    assert 'id' not in r['products'][0] and 'backendProductId' not in r['products'][0]


@pytest.mark.parametrize('invalid_id', [True, False, 1.0, '1', 0, -1, 9223372036854775808])
def test_direct_product_lookup_rejects_non_positive_int64_ids(service, invalid_id):
    with pytest.raises(ValueError):
        service.get_products([invalid_id])


async def test_pagination_is_stable_without_duplicates(service):
    a=await service.search({'limit':2,'offset':0})
    b=await service.search({'limit':2,'offset':2})
    assert not {p['productId'] for p in a['hits']} & {p['productId'] for p in b['hits']}
    assert a['candidateCount']==5 and b['hasMore']


def test_corrupt_vectors_are_rejected(service,tmp_path):
    with (tmp_path/'vectors.f32').open('r+b') as f:f.write(b'xxxx')
    with pytest.raises(ValueError,match='hash_mismatch'):SearchService(tmp_path)


async def test_kiwi_particles_keep_results_scores_and_matching_fields(service):
    base=await service.search({'query':'텀블러','mode':'lexical'})
    inflected=await service.search({'query':'텀블러를','mode':'lexical'})
    assert base['hits']==inflected['hits']
    assert base['hits'] and '상품명' in inflected['hits'][0]['matchedFields']
    assert service.get_metadata()['lexicalTokenizer']['name']=='kiwi'


async def test_nonmatching_character_fragment_does_not_retrieve_a_tumbler(service):
    result=await service.search({'query':'블러','mode':'lexical'})
    assert result['hits']==[]
