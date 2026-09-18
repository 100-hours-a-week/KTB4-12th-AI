from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class SearchOutcome(str, Enum):
    MATCHES = "MATCHES"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class SearchFilters:
    """검색 강제 필터 조건 (예산, 비선호/제외 카테고리 등)"""
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    include_category_ids: Tuple[int, ...] = ()
    exclude_category_ids: Tuple[int, ...] = ()
    exclude_product_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class SearchRequest:
    """검색 요청 DTO"""
    query: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    limit: int = 5
    preferences: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductEvidence:
    """검색 결과 상품의 정본 근거 정보"""
    product_id: int
    name: str
    price: int
    category_id: int
    category_name: str
    description: str
    brand: Optional[str] = None
    available: bool = True


@dataclass(frozen=True)
class SearchHit:
    """단일 검색 매칭 결과"""
    product_id: int
    rank: int
    score: float
    evidence: ProductEvidence


@dataclass(frozen=True)
class SearchResult:
    """검색 최종 결과 DTO"""
    snapshot_id: str
    hits: Tuple[SearchHit, ...]
    outcome: SearchOutcome
    total_count: int
