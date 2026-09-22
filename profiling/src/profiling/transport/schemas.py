"""API 경계 DTO — 모델 API 설계 v3.2.7의 7.6 · 7.7 · 7.9를 그대로 옮긴다.

필드명은 문서와 1:1(camelCase). 문서가 바뀌면 이 파일만 바뀐다.
내부 자료형(snake_case)은 profile/types.py, catalog/types.py에 있고, 변환은 api/ 안에서만 한다.

구성: 1) 상수·타입 별칭  2) 공통 봉투  3) 7.6  4) 7.7  5) 7.9
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 1) 상수 · 타입 별칭 — 클래스 필드가 아님. 응답 본문에 들어가는 값의 "허용 범위"를 정의한다.
# ---------------------------------------------------------------------------

## §2.3 프로파일 상태 모델. AI가 보내는 값은 7.6 응답 PENDING, 7.7 요청 COMPLETED뿐
ProfileStatus = Literal["NONE", "PENDING", "COMPLETED", "FAILED"]

## 7.6 오류 코드 (7.5 채팅과 같은 체계). 분석이 실패해도 이 엔드포인트나 7.7로 FAILED를 보내지 않는다
ProfileExtractErrorCode = Literal[
    "INVALID_REQUEST",  # 400 — 필수 필드 누락·타입 오류·reviews 10개 초과·rating 범위 밖. Backend 재시도 안 함
    "UNAUTHORIZED",  # 401 — 서비스 토큰 오류
    "FORBIDDEN",  # 403 — 접근 권한 없음
    "INTERNAL_SERVER_ERROR",  # 500 — Backend가 다음 디바운스 주기에 재시도
    "SERVICE_UNAVAILABLE",  # 503 — 활성 카탈로그 없음·모델 서버 다운. Backend가 다음 주기에 재시도
]

## 7.7 오류 코드 — AI가 콜백을 보낸 뒤 Backend에게 받는 것
ProfileCallbackErrorCode = Literal[
    "RECIPIENT_ID_MISMATCH",  # 400 — 재시도하지 않음. AI 쪽 버그로 간주
    "INVALID_REQUEST",  # 400 — 재시도하지 않음
    "STALE_SOURCE_VERSION",  # 409 — 순서 역전. 재시도하지 않고 결과 폐기(SUPERSEDED)
    "UNAUTHORIZED",  # 401
    "FORBIDDEN",  # 403
    "INTERNAL_SERVER_ERROR",  # 500 — 최대 3회 재시도
    "SERVICE_UNAVAILABLE",  # 503 — 최대 3회 재시도
]


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
# 3) 7.6 수신자 비동기 프로파일링 웹훅 — Backend → AI
# POST /api/internal/v1/ai/profile/extract-and-pool
# ---------------------------------------------------------------------------


## 비선호 카테고리
class DislikedCategoryDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categoryId: int = Field(gt=0, description="안전한 양의 정수; 감점 대상 판정 키")
    categoryName: str = Field(description="LLM이 의미를 이해하는 용도")


## 리뷰
class ReviewDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productId: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    reviewText: str | None = Field(default=None, description="리뷰 본문. 글이 없으면 null (별점 필수·글 선택)")


## 프로파일 추출 요청 — giftPreference만 null 허용, 나머지는 값이 없어도 []·0으로 온다
class ProfileExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipientUserId: int = Field(gt=0)
    sourceVersion: int = Field(ge=0, description="호출 시점의 최신 값. 등록 데이터가 없으면 0")
    dislikedCategories: list[DislikedCategoryDto] = Field(description="없으면 []; 필드 생략 불가")
    giftPreference: str | None = Field(description="취향 자유 텍스트; 없으면 null")
    reviews: list[ReviewDto] = Field(max_length=10, description="작성일 최신순 최대 10개; 없으면 []")


## 프로파일 접수 응답 data (202) — 요청 값을 그대로 돌려주고 profileStatus는 항상 PENDING
class ProfileAccepted(BaseModel):
    recipientUserId: int
    sourceVersion: int
    profileStatus: Literal["PENDING"] = "PENDING"


# ---------------------------------------------------------------------------
# 4) 7.7 30개 추천풀 콜백 저장 — AI → Backend
# POST /api/internal/v1/recipients/{recipientUserId}/profile
# ---------------------------------------------------------------------------


## 프로파일 콜백 요청 — 태그는 보내지 않는다(AI ai_profile.recipient_profiles에 보관, DR-035).
## Body의 recipientUserId는 Path의 {recipientUserId}와 같아야 한다(다르면 400 RECIPIENT_ID_MISMATCH).
## recommendedProductIds는 배열 순서가 곧 추천 순위, 최대 30개.
class ProfileCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipientUserId: int = Field(gt=0)
    sourceVersion: int = Field(ge=0, description="7.6 요청으로 받은 값을 그대로")
    profileStatus: Literal["COMPLETED"] = "COMPLETED"
    recommendedProductIds: list[int] = Field(max_length=30)


## 프로파일 콜백 응답 data (200)
class ProfileCallbackAccepted(BaseModel):
    recipientUserId: int
    sourceVersion: int
    profileStatus: Literal["COMPLETED"]


# ---------------------------------------------------------------------------
# 5) 7.9 상품 전체 export — Backend → AI Catalog CLI
# GET /internal/v1/ai/products/export
# ---------------------------------------------------------------------------


## 상품 레코드 — data.products[] 한 건. description만 null 허용
class ProductRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productId: int = Field(gt=0)
    name: str
    brand: str
    description: str | None
    categoryId: int = Field(gt=0)
    categoryName: str
    price: int = Field(ge=0, description="현재 가격. export 시점의 값이며 결제 근거가 아님")
    available: bool = Field(description="quantity > 0. 재고 0인 상품도 false로 포함됨")
    updatedAt: datetime = Field(description="상품·카테고리 updated_at 중 늦은 값 (UTC ISO 8601)")
    viewCount: int = Field(default=0, ge=0, description="조회수 — v1 추천 풀 정렬 기준(내림차순). Backend가 초기에는 임의 값을 넣어 보냄(09-22 합의). "
                                                       "필드명은 Backend 확정 전 임시 — 바뀌면 여기 한 곳. 없으면 0")


## 상품 export 응답 data — 삭제되지 않고 카테고리가 유효한 전체 상품, productId 오름차순. 비면 products: []
class ProductExportData(BaseModel):
    generatedAt: datetime
    products: list[ProductRecord]


ProductExportResponse = SuccessResponse[ProductExportData]
