import pytest
from search_catalog.search.service import SearchService
from search_catalog.search.types import SearchFilters, SearchOutcome, SearchRequest


@pytest.mark.asyncio
async def test_search_basic():
    service = SearchService()
    req = SearchRequest(query="선물 추천", limit=5)
    res = await service.search(req)

    assert res.outcome == SearchOutcome.MATCHES
    assert len(res.hits) > 0
    assert res.total_count == len(res.hits)


@pytest.mark.asyncio
async def test_search_price_filter():
    service = SearchService()
    # 40000원 이하 필터
    req = SearchRequest(
        query="저렴한 선물",
        filters=SearchFilters(max_price=40000),
        limit=5,
    )
    res = await service.search(req)

    assert res.outcome == SearchOutcome.MATCHES
    for hit in res.hits:
        assert hit.evidence.price <= 40000


@pytest.mark.asyncio
async def test_search_no_match_price():
    service = SearchService()
    # 1000원 이하 상품 없음
    req = SearchRequest(
        query="너무 싼 선물",
        filters=SearchFilters(max_price=1000),
        limit=5,
    )
    res = await service.search(req)

    assert res.outcome == SearchOutcome.NO_MATCH
    assert len(res.hits) == 0
