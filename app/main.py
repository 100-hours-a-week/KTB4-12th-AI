from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional

from app.config import settings
from app.search.service import SearchService
from app.search.types import SearchFilters, SearchRequest, SearchResult

app = FastAPI(
    title="선잘알 AI Backend Service",
    description="선물 추천 및 대화 검색 API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

search_service = SearchService()


class SearchFilterBody(BaseModel):
    minPrice: Optional[int] = Field(None, description="최소 가격")
    maxPrice: Optional[int] = Field(None, description="최대 가격")
    includeCategoryIds: List[int] = Field(default_factory=list)
    excludeCategoryIds: List[int] = Field(default_factory=list)
    excludeProductIds: List[int] = Field(default_factory=list)


class SearchRequestBody(BaseModel):
    query: str = Field(..., description="검색 쿼리")
    filters: SearchFilterBody = Field(default_factory=SearchFilterBody)
    limit: int = Field(5, ge=1, le=50)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "app": settings.app_name,
        "env": settings.app_env,
    }


@app.post("/api/v1/search")
async def search_endpoint(body: SearchRequestBody):
    req = SearchRequest(
        query=body.query,
        filters=SearchFilters(
            min_price=body.filters.minPrice,
            max_price=body.filters.maxPrice,
            include_category_ids=tuple(body.filters.includeCategoryIds),
            exclude_category_ids=tuple(body.filters.excludeCategoryIds),
            exclude_product_ids=tuple(body.filters.excludeProductIds),
        ),
        limit=body.limit,
    )
    result: SearchResult = await search_service.search(req)
    return {
        "message": "검색이 완료되었습니다.",
        "data": {
            "snapshotId": result.snapshot_id,
            "totalCount": result.total_count,
            "hits": [
                {
                    "productId": hit.evidence.product_id,
                    "name": hit.evidence.name,
                    "price": hit.evidence.price,
                    "categoryId": hit.evidence.category_id,
                    "categoryName": hit.evidence.category_name,
                    "description": hit.evidence.description,
                    "brand": hit.evidence.brand,
                    "score": hit.score,
                    "rank": hit.rank,
                }
                for hit in result.hits
            ],
        },
    }
