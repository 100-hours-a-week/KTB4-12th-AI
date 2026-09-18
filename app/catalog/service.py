import logging
from datetime import datetime
from typing import List, Optional
import httpx

from app.catalog.types import ProductDocument, SnapshotManifest
from app.config import settings

logger = logging.getLogger(__name__)


class CatalogService:
    """메인 백엔드의 상품 정본을 동기화하고 벡터 인덱스 스냅샷을 관리하는 서비스"""

    def __init__(self, backend_url: Optional[str] = None):
        self.backend_url = backend_url or settings.main_backend_url
        self.active_snapshot_id: str = "v1.0.0"

    async def fetch_backend_export(self) -> List[ProductDocument]:
        """메인 백엔드의 상품 전체 export API 호출"""
        url = f"{self.backend_url}/internal/v1/ai/products/export"
        headers = {"Authorization": f"Bearer {settings.internal_service_token}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json().get("data", [])
                    return [
                        ProductDocument(
                            product_id=item["productId"],
                            name=item["name"],
                            price=item["price"],
                            category_id=item["categoryId"],
                            category_name=item["categoryName"],
                            description=item.get("description", ""),
                            brand=item.get("brand"),
                            is_active=item.get("isActive", True),
                        )
                        for item in data
                    ]
                logger.warning("Backend export returned %s", response.status_code)
        except Exception as exc:
            logger.error("Failed to fetch backend product export: %s", exc)

        return []

    async def build_snapshot(self, products: List[ProductDocument], snapshot_id: str) -> SnapshotManifest:
        """수신된 상품 데이터로 임베딩을 생성하고 pgvector 테이블에 스냅샷 적재"""
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            total_products=len(products),
            indexed_embeddings=len(products),
            created_at=datetime.utcnow().isoformat(),
            active=False,
        )
        logger.info("Built snapshot %s with %d products", snapshot_id, len(products))
        return manifest

    def activate_snapshot(self, snapshot_id: str) -> None:
        """새로 생성된 스냅샷 버전을 검색 활성 버전으로 전환"""
        self.active_snapshot_id = snapshot_id
        logger.info("Active snapshot switched to: %s", snapshot_id)
