from typing import Protocol, Tuple
from search_catalog.search.types import SearchHit, SearchRequest


class RetrievalPort(Protocol):
    """PostgreSQL pgvector 또는 검색 엔진 어댑터 추상 인터페이스"""
    async def retrieve(
        self, request: SearchRequest, snapshot_id: str
    ) -> Tuple[SearchHit, ...]:
        ...
