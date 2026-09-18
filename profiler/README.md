# 수신자 프로파일러 (`profiler/`)

수신자의 취향, 비선호 카테고리, 선물 리뷰를 분석하고 추천 상품 풀(30개)을 생성하는 동료 전용 작업 공간입니다.

## 검색 엔진 연동 방법

검색 모듈(`search_catalog.search`)의 `SearchService`를 주입받아 사용합니다.

```python
from search_catalog.search.service import SearchService
from search_catalog.search.types import SearchRequest, SearchFilters

search_service = SearchService()

# 30개 추천 풀 검색 예시
request = SearchRequest(
    query="커피 디퓨저 선호",
    filters=SearchFilters(
        max_price=50000,
        exclude_category_ids=(10, 20)  # 수신자의 비선호 카테고리 배제
    ),
    limit=30
)

result = await search_service.search(request)
product_ids = [hit.product_id for hit in result.hits]
```
