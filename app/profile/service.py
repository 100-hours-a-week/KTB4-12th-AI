import logging
from typing import List, Optional
from app.search.service import SearchService
from app.search.types import SearchFilters, SearchRequest

logger = logging.getLogger(__name__)


class ProfileService:
    """
    수신자 취향 및 리뷰 분석 서비스 (Emet 담당 영역)
    - 취향/비선호/리뷰 분석 후 태그 추출
    - SearchService를 주입받아 추천 후보군(최대 30개) 구성
    - 메인 백엔드로 콜백 전송
    """

    def __init__(self, search_service: Optional[SearchService] = None):
        self.search_service = search_service or SearchService()

    async def analyze_recipient_and_recommend(
        self,
        recipient_user_id: int,
        source_version: int,
        taste_tags: List[str],
        exclude_category_ids: List[int],
    ) -> List[int]:
        query = " ".join(taste_tags) if taste_tags else "인기 선물 추천"
        request = SearchRequest(
            query=query,
            filters=SearchFilters(
                exclude_category_ids=tuple(exclude_category_ids)
            ),
            limit=30,
        )

        result = await self.search_service.search(request)
        recommended_ids = [hit.product_id for hit in result.hits]
        logger.info(
            "Generated %d recommendations for recipient %d",
            len(recommended_ids),
            recipient_user_id,
        )
        return recommended_ids
