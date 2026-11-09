"""API 경계 DTO — 공통 오류 봉투와 모델 API 설계 v3.2.7의 7.9(상품 export)를 그대로 옮긴다.

필드명은 문서와 1:1(camelCase). 문서가 바뀌면 이 파일만 바뀐다. 내부 자료형(snake_case)은 types.py에 있다.
7.6 · 7.7(HTTP 프로파일링 웹훅 · 콜백)은 11-08 RabbitMQ 로 대체하며 지웠다 — 요청 · 결과 메시지 규격은 mq/messages.py · mq/results.py.

구성: 1) 상수·타입 별칭  2) 공통 봉투  3) 7.9
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# 1) 상수 · 타입 별칭 — 클래스 필드가 아님. 응답 본문에 들어가는 값의 "허용 범위"를 정의한다.
# ---------------------------------------------------------------------------

## 재고 상태 — 검색기(product-search)와 같은 3값. 미확인은 unknown으로 두고 추정하지 않는다.
## 패키지로 적재한 상품은 재고 정보가 없어 전건 unknown이고, Backend 7.9 export가 available/unavailable로 덮는다.
Availability = Literal["available", "unavailable", "unknown"]



# ---------------------------------------------------------------------------
# 2) 공통 봉투 (§3 오류 응답 · SuccessResponse<T>)
# ---------------------------------------------------------------------------


class SuccessResponse[T](BaseModel):  # PEP 695 제네릭 (Python 3.12+)
    """`{message, data}` — 성공 응답 공통 봉투."""

    message: str
    data: T


class ErrorBody(BaseModel):
    code: str
    traceId: str | None = None


class ErrorResponse(BaseModel):
    """`{message, error: {code, traceId}}` — 오류 응답 공통 봉투."""

    message: str
    error: ErrorBody


# ---------------------------------------------------------------------------
# 3) 7.9 상품 전체 export — Backend → AI Catalog CLI
# GET /internal/v1/ai/products/export
# ---------------------------------------------------------------------------


## 상품 레코드 — BE 관측 필드 검증을 유지하며, Search snapshot context에서만 미확인 관측값을 받는다.
## 모르는 필드는 무시(extra="ignore") — Backend가 필드를 더 보내도 상품이 버려지지 않게.
class ProductRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    productId: int = Field(gt=0)
    name: str
    brand: str
    description: str | None
    categoryId: int = Field(gt=0)
    categoryName: str
    parentCategoryId: int | None = Field(
        default=None, gt=0,
        description="대분류 Backend id(categories.id). 7.9 export에는 없고 DB 카탈로그가 categories 부모 조인으로 채운다. "
                    "비선호는 대분류로 오므로(09-27 BE 결정: PreferenceSaveService 가 root만 허용) 비선호 판정(v1 제외 · v2 감점)에 쓴다. "
                    "파일 카탈로그는 없으면 None")
    parentCategoryName: str | None = Field(default=None, description="대분류 이름 — 이름 보조 판정용")
    productKind: str | None = Field(default=None, description="상품 종류(product_kind, 727종 — 예: 공용향수 · 헤드폰). 태그 추출 프롬프트가 쓴다. "
                                                          "DB 카탈로그가 채우고 7.9 export · 파일 카탈로그엔 없으면 None")
    price: int = Field(ge=0, description="현재 가격. export 시점의 값이며 결제 근거가 아님")
    availability: Availability = Field(
        default="unknown", validation_alias=AliasChoices("availability", "available"),
        description="재고 상태 3값(검색기와 같은 형). 7.9는 boolean available로 보내므로 true→available · false→unavailable로 받고, "
                    "필드가 없거나 null이면 unknown(=재고 정보가 없는 상품). unknown을 available/unavailable로 추정하지 않는다")
    updatedAt: datetime | None = Field(description="상품·카테고리 updated_at 중 늦은 값 (UTC ISO 8601). 검증된 Search 계약의 명시적 null은 미관측")
    ## v1이후 삭제 예정
    viewCount: int | None = Field(default=0, ge=0, validation_alias=AliasChoices("viewCount", "views", "view_count"),
                           description="조회수 — v1 추천 풀 정렬 기준(내림차순). Backend가 초기에는 임의 값을 넣어 보냄(09-22 합의). "
                                       "Backend 열 이름이 views라 viewCount·views·view_count 어느 이름으로 와도 받는다. 없으면 0. "
                                       "검증된 Search snapshot/export의 명시적 null은 관측값 없음이며 인기 점수에 쓰지 않는다")
    tags: frozenset[tuple[str, str]] | None = Field(
        default=None, exclude=True,
        description="상품 축 태그 (축, 대표 이름) 집합 — DB 카탈로그가 판(tags_rule · tags_sha256)이 코드와 같을 때만 채운다(0011). "
                    "None 이면 vocab 이 같은 함수로 그 자리에서 계산한다. 계약 필드가 아니라 응답 · export 에는 나가지 않는다")

    @field_validator("availability", mode="before")
    @classmethod
    def _from_7_9_boolean(cls, v: object) -> object:
        """7.9의 boolean을 3값으로. 명시적 null도 unknown — Backend가 "모른다"를 null로 보낼 수 있다."""
        if isinstance(v, bool):
            return "available" if v else "unavailable"
        return "unknown" if v is None else v

    @model_validator(mode="after")
    def _source_signals(self, info: ValidationInfo):
        # Only the bound Search contract can allow unknown ranking metadata.
        # Direct Backend/file validation retains its existing required fields.
        source = info.context.get("catalog_source") if isinstance(info.context, dict) else None
        if source in {"catalog-snapshot", "backend-export"} and not {"updatedAt", "viewCount"} <= self.model_fields_set:
            raise ValueError("Search observation fields must be explicit")
        if source == "catalog-snapshot":
            if self.updatedAt is not None or self.viewCount is not None or self.availability != "unknown":
                raise ValueError("Snapshot observation metadata must remain unknown")
        elif source != "backend-export" and (self.updatedAt is None or self.viewCount is None):
            raise ValueError("Observed catalog metadata required")
        return self


PRODUCT_FIELDS = frozenset(ProductRecord.model_fields) - {"tags"}                        # 계약 필드 이름 (CLI 보고용) — tags 는 DB 전용
PRODUCT_FIELD_ALIASES = frozenset({"views", "view_count", "available"})                   # 같은 뜻의 다른 이름 — 모르는 필드로 세지 않음


## 상품 export 응답 data — 삭제되지 않고 카테고리가 유효한 전체 상품, productId 오름차순. 비면 products: []
class ProductExportData(BaseModel):
    generatedAt: datetime
    products: list[ProductRecord]


ProductExportResponse = SuccessResponse[ProductExportData]
