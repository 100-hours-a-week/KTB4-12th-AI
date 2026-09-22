"""수신자 프로필 — 태그 추출 결과를 AI DB(ai_profile.recipient_profiles)에 보관·갱신하는 업무 규칙. (목 — 함수 이름만, 내용은 채울 것)

근거: 담당파트 상세설계서 §1.7 (태그는 AI가 보관, 한 사람당 한 행, 최신 분석이 덮어씀) · 5단계 1.2 · 3단계 구현 상세 §10.2.
v1에서는 태그가 비선호 카테고리 이름뿐이지만, v3에서 종류·특징·싫어함 3축이 들어오면 이 파일의 규칙으로 행을 만들고 갱신한다.

테이블 한 행(RecipientProfile) — 컬럼 이름은 설계서 그대로(snake_case):
  recipient_user_id PK · source_version · profile_run_id · preferred_tags[≤12] · disliked_tags[≤8] · axes{likes,key_features,dislikes}
  · disliked_categories[{category_id, category_name}] · recommended_product_ids[≤30] · catalog_version_id · prompt_version · validator_version · updated_at

  DB(마이그레이션 0001)에 지금 있는 열: recipient_user_id · source_version · preferred_tags · disliked_tags · disliked_categories · updated_at.
  나머지(profile_run_id · axes · recommended_product_ids · catalog_version_id · prompt/validator_version)는 v3 마이그레이션 0003에서 —
  그 전까지 DbRecipientProfileStore는 이 필드들을 저장하지 않는다(객체에는 들어 있음).

채워진 것(09-22, DB 연결에 필요해서):
  - from_outcome · should_replace · cap_tags
채울 것(v3):
  - def merge_explicit_dislikes(profile, category_names) -> RecipientProfile
        Backend 명시 비선호를 disliked_tags 앞에 두고 중복 제거, 상한 8 (검증기 병합 규칙과 동일)
  - def to_search_hint(profile) -> dict                       Chat/Search가 쓰는 형태: {query_terms, category_boost, penalty_tags} — v3에서 Search 인자와 맞춤
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from profiling.profile.types import (
    DislikedCategory,
    ProfileOutcome,
    ProfileRequest,
    ValidatedTag,
)

MAX_PREFERRED_TAGS = 12
MAX_DISLIKED_TAGS = 8


class RecipientProfile(BaseModel):
    """ai_profile.recipient_profiles 한 행."""

    recipient_user_id: int
    source_version: int
    profile_run_id: str | None = None
    preferred_tags: list[str] = Field(default_factory=list, max_length=MAX_PREFERRED_TAGS)
    disliked_tags: list[str] = Field(default_factory=list, max_length=MAX_DISLIKED_TAGS)
    axes: dict[str, list[ValidatedTag]] = Field(default_factory=lambda: {"likes": [], "key_features": [], "dislikes": []})
    disliked_categories: list[DislikedCategory] = Field(default_factory=list)   # 7.6에서 받은 명시 비선호 — 분석 시점 사본 (테이블 열과 같은 이름)
    recommended_product_ids: list[int] = Field(default_factory=list, max_length=30)
    catalog_version_id: UUID | None = None
    prompt_version: str | None = None
    validator_version: str | None = None
    updated_at: datetime | None = None


def from_outcome(rq: ProfileRequest, outcome: ProfileOutcome, profile_run_id: str | None = None) -> RecipientProfile:
    """ProfileOutcome(+요청) → RecipientProfile 한 행. pipeline.profile()이 RESULT_READY 저장 직후 부른다.

    - preferred/disliked 태그: outcome.validation의 2필드를 상한(12·8)으로 자른다. v1은 disliked = 비선호 카테고리 이름뿐.
    - axes: ValidationResult의 3축 원형(v1은 전부 []).
    - disliked_categories: 요청(rq)에서 그대로 — "분석 시점 사본"이라 Backend가 이후 바꿔도 다음 7.6까지 모른다(설계서 §1.7).
    - recommended_product_ids · catalog_version_id: outcome.search에서(콜백과 같은 30개).
    """
    v = outcome.validation
    s = outcome.search
    return RecipientProfile(
        recipient_user_id=rq.recipient_user_id,
        source_version=rq.source_version,
        profile_run_id=profile_run_id,
        preferred_tags=cap_tags(v.preferred_tags, MAX_PREFERRED_TAGS) if v else [],
        disliked_tags=cap_tags(v.disliked_tags, MAX_DISLIKED_TAGS) if v else [],
        axes={"likes": list(v.likes), "key_features": list(v.key_features), "dislikes": list(v.dislikes)} if v
             else {"likes": [], "key_features": [], "dislikes": []},
        disliked_categories=list(rq.disliked_categories),
        recommended_product_ids=list(s.product_ids) if s else [],
        catalog_version_id=s.catalog_version_id if s else None,
        prompt_version=outcome.prompt_version,
        validator_version=outcome.validator_version,
    )


def should_replace(current: RecipientProfile | None, incoming: RecipientProfile) -> bool:
    """기존 행을 새 결과로 덮어쓸지 — source_version이 같거나 높을 때만. 낮으면 순서 역전(늦게 도착한 옛 분석) → 기존 유지.

    DB adapter의 upsert도 같은 규칙을 SQL(WHERE source_version <= excluded.source_version)로 한 번 더 건다 — 두 프로세스가
    동시에 써도 DB가 최종 심판. 이 함수는 메모리 구현과 호출자의 사전 판단용.
    """
    return current is None or current.source_version <= incoming.source_version


def merge_explicit_dislikes(profile: RecipientProfile, category_names: list[str]) -> RecipientProfile:
    """Backend 명시 비선호를 disliked_tags 앞에 병합 (중복 제거·상한)."""
    raise NotImplementedError


def cap_tags(tags: list[str], limit: int) -> list[str]:
    """중복 제거(앞의 것을 남김 — 검증기가 확정 우선으로 정렬해 두었으므로) 후 앞에서 limit개. 빈 문자열·공백은 버린다."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        t = tag.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def to_search_hint(profile: RecipientProfile) -> dict:
    """Chat/Search가 쓰는 형태로 변환 — v3에서 Search 인자와 맞춘다."""
    raise NotImplementedError
