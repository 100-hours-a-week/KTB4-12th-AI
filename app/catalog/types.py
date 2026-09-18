from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ProductDocument:
    """메인 백엔드로부터 수신한 단일 상품 정본 데이터"""
    product_id: int
    name: str
    price: int
    category_id: int
    category_name: str
    description: str
    brand: Optional[str] = None
    is_active: bool = True
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class SnapshotManifest:
    """버전별 카탈로그 스냅샷 메타데이터"""
    snapshot_id: str
    total_products: int
    indexed_embeddings: int
    created_at: str
    active: bool = False
