from typing import Optional, Tuple
from app.search.types import (
    ProductEvidence,
    SearchHit,
    SearchOutcome,
    SearchRequest,
    SearchResult,
)
from app.search.ports import RetrievalPort


class MockRetrievalAdapter:
    """초기 개발 및 테스트용 Mock 검색 어댑터"""
    async def retrieve(
        self, request: SearchRequest, snapshot_id: str
    ) -> Tuple[SearchHit, ...]:
        dummy_products = [
            ProductEvidence(
                product_id=1001,
                name="프리미엄 세라믹 머그컵 세트",
                price=32000,
                category_id=10,
                category_name="테이블웨어",
                description="고급스러운 마감과 보온성을 갖춘 2인 머그 세트",
                brand="오덴세",
                available=True,
            ),
            ProductEvidence(
                product_id=1002,
                name="아로마 디퓨저 & 룸스프레이 기프트 세트",
                price=45000,
                category_id=20,
                category_name="인테리어/방향",
                description="은은한 숲향으로 편안한 무드를 연출하는 디퓨저",
                brand="이솝",
                available=True,
            ),
            ProductEvidence(
                product_id=1003,
                name="천연 소가죽 카드지갑",
                price=68000,
                category_id=30,
                category_name="패션잡화",
                description="슬림하고 실용적인 수납공간을 갖춘 미니멀 지갑",
                brand="매드고트",
                available=True,
            ),
        ]

        hits = []
        rank = 1
        for prod in dummy_products:
            if request.filters.max_price and prod.price > request.filters.max_price:
                continue
            if request.filters.min_price and prod.price < request.filters.min_price:
                continue
            if prod.category_id in request.filters.exclude_category_ids:
                continue
            if prod.product_id in request.filters.exclude_product_ids:
                continue

            hits.append(SearchHit(product_id=prod.product_id, rank=rank, score=0.95 - (rank * 0.05), evidence=prod))
            rank += 1
            if len(hits) >= request.limit:
                break

        return tuple(hits)


class SearchService:
    """상품 탐색 및 검색 코어 서비스"""

    def __init__(self, retrieval_adapter: Optional[RetrievalPort] = None):
        self.retrieval = retrieval_adapter or MockRetrievalAdapter()

    async def search(self, request: SearchRequest, snapshot_id: str = "v1.0.0") -> SearchResult:
        hits = await self.retrieval.retrieve(request, snapshot_id)

        outcome = SearchOutcome.MATCHES if hits else SearchOutcome.NO_MATCH
        return SearchResult(
            snapshot_id=snapshot_id,
            hits=hits,
            outcome=outcome,
            total_count=len(hits),
        )
