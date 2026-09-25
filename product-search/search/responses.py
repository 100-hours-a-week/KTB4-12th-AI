"""Public response contracts shared by validation and OpenAPI."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import CategoryId, ProductId, SearchRequest

Count = Annotated[int, Field(ge=0)]
Rank = Annotated[int, Field(ge=1)]
Timestamp = Annotated[str, Field(json_schema_extra={'format': 'date-time'})]


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ProductSummary(ResponseModel):
    productId: ProductId
    sourceProductId: str | None
    name: str
    brand: str
    categoryId: CategoryId
    sourceCategoryId: str | None
    parentCategoryId: CategoryId
    category: str
    categoryGroup: str
    price: Count
    description: str
    productType: Literal['Shipping', 'Pickup', 'Voucher'] | None
    availability: Literal['available', 'unavailable', 'unknown']
    image: str
    imageLarge: str
    imageFallback: str


class SearchHit(ProductSummary):
    rank: Rank
    score: float
    lexicalScore: float
    denseScore: float | None
    lexicalRank: Rank | None
    denseRank: Rank | None
    matchedFields: list[str]


class SearchTiming(ResponseModel):
    totalMs: float
    embeddingMs: float
    retrievalMs: float
    cached: bool
    embeddingCached: bool


class SearchResponse(ResponseModel):
    hits: list[SearchHit]
    eligibleCount: Count
    candidateCount: Count
    hasMore: bool
    nextOffset: Count | None
    status: Literal['OK', 'NO_MATCH']
    snapshotId: str
    algorithm: str
    timing: SearchTiming


class QASearchResponse(SearchResponse):
    searchId: str
    request: SearchRequest


class HistoricalQASearchResponse(QASearchResponse):
    # Records produced before cursor support have no nextOffset. The replay route
    # excludes unset fields rather than inventing a cursor for historical output.
    nextOffset: Count | None = None


class ProductDetail(ProductSummary):
    attributes: dict[str, Any]
    tags: list[str]
    sourceText: str
    productUrl: str
    descriptionOrigin: str
    tokenCount: Count
    embeddingTruncated: bool


class ProductsResponse(ResponseModel):
    snapshotId: str
    products: list[ProductDetail]
    missingIds: list[ProductId]


class LexicalTokenizer(ResponseModel):
    name: str
    version: str
    modelVersion: str
    modelType: str
    workers: Count
    policy: str


class CategoryMetadata(ResponseModel):
    id: CategoryId
    name: str
    group: str
    parentId: CategoryId
    sourceCategoryId: str | None
    count: Count


class GroupMetadata(ResponseModel):
    id: CategoryId
    name: str
    sourceCategoryId: str | None
    count: Count


class BrandMetadata(ResponseModel):
    name: str
    count: Count


class ProductTypeMetadata(ResponseModel):
    id: Literal['Shipping', 'Pickup', 'Voucher'] | None
    count: Count


class MetadataResponse(ResponseModel):
    snapshotId: str
    algorithm: str
    productCount: Count
    lexicalTokenizer: LexicalTokenizer
    categories: list[CategoryMetadata]
    groups: list[GroupMetadata]
    brands: list[BrandMetadata]
    productTypes: list[ProductTypeMetadata]
    model: str
    dimensions: Count
    maxTokens: Count
    availabilityNote: str
    source: str
    exportGeneratedAt: Timestamp | None


class ReadyResponse(ResponseModel):
    status: Literal['ready']
    products: Count
    snapshotId: str
    catalogLoadError: str | None
    exportGeneratedAt: Timestamp | None


class FeedbackResponse(ResponseModel):
    id: str
    saved: Literal[True]


ErrorCode = Literal[
    'INVALID_REQUEST', 'UNKNOWN_CATEGORY', 'UNKNOWN_BRAND', 'SNAPSHOT_MISMATCH',
    'SEARCH_BUSY', 'SEARCH_TIMEOUT', 'ENCODER_FAILURE', 'NOT_FOUND', 'SEARCH_NOT_READY',
]


class ValidationIssue(ResponseModel):
    field: str
    message: str


class ErrorBody(ResponseModel):
    code: ErrorCode
    issues: list[ValidationIssue] | None = None


class APIErrorResponse(ResponseModel):
    message: str
    error: ErrorBody
