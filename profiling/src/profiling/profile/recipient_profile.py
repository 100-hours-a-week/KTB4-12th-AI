"""수신자 프로필 — 태그 추출 결과를 AI DB(ai_profile.recipient_profiles)에 보관·갱신하는 업무 규칙. (목 — 함수 이름만, 내용은 채울 것)

근거: 담당파트 상세설계서 §1.7 (태그는 AI가 보관, 한 사람당 한 행, 최신 분석이 덮어씀) · 5단계 1.2 · 3단계 구현 상세 §10.2.
v1에서는 태그가 비선호 카테고리 이름뿐이지만, v3에서 종류·특징·싫어함 3축이 들어오면 이 파일의 규칙으로 행을 만들고 갱신한다.

테이블 한 행(RecipientProfile) — 컬럼 이름은 설계서 그대로(snake_case):
  recipient_user_id PK · source_version · profile_run_id · preferred_tags[≤12] · disliked_tags[≤8] · axes{likes,key_features,dislikes}
  · explicit_disliked_category_ids · recommended_product_ids[≤30] · catalog_version_id · prompt_version · validator_version · updated_at

여기에 쓸 것:
  - class RecipientProfile(BaseModel)                        위 컬럼 그대로. axes는 ValidationResult의 3축(ValidatedTag) 원형
  - def from_outcome(rq, outcome, profile_run_id) -> RecipientProfile
        ProfileOutcome(+요청 rq) → 행 하나. preferred/disliked는 outcome.validation의 2필드, explicit_*는 rq.disliked_categories의 ID
  - def should_replace(current, incoming) -> bool
        같은 수신자의 기존 행을 새 결과로 덮어쓸지 — source_version이 같거나 높을 때만(낮으면 순서 역전 → 유지). 실행 기록의 SUPERSEDED와 짝
  - def merge_explicit_dislikes(profile, category_names) -> RecipientProfile
        Backend 명시 비선호를 disliked_tags 앞에 두고 중복 제거, 상한 8 (검증기 병합 규칙과 동일)
  - def cap_tags(tags, limit) -> list[str]                     확정 우선·중복 제거·상한 (preferred 12 · disliked 8)
  - def to_search_hint(profile) -> dict                       Chat/Search가 쓰는 형태: {query_terms, category_boost, penalty_tags} — v3에서 Search 인자와 맞춤
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from profiling.profile.types import ProfileOutcome, ProfileRequest, ValidatedTag

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
    explicit_disliked_category_ids: list[int] = Field(default_factory=list)
    recommended_product_ids: list[int] = Field(default_factory=list, max_length=30)
    catalog_version_id: UUID | None = None
    prompt_version: str | None = None
    validator_version: str | None = None
    updated_at: datetime | None = None


def from_outcome(rq: ProfileRequest, outcome: ProfileOutcome, profile_run_id: str | None = None) -> RecipientProfile:
    """ProfileOutcome(+요청) → RecipientProfile 한 행."""
    raise NotImplementedError


def should_replace(current: RecipientProfile | None, incoming: RecipientProfile) -> bool:
    """기존 행을 새 결과로 덮어쓸지 — source_version 순서 규칙."""
    raise NotImplementedError


def merge_explicit_dislikes(profile: RecipientProfile, category_names: list[str]) -> RecipientProfile:
    """Backend 명시 비선호를 disliked_tags 앞에 병합 (중복 제거·상한)."""
    raise NotImplementedError


def cap_tags(tags: list[str], limit: int) -> list[str]:
    """중복 제거 후 앞에서 limit개."""
    raise NotImplementedError


def to_search_hint(profile: RecipientProfile) -> dict:
    """Chat/Search가 쓰는 형태로 변환 — v3에서 Search 인자와 맞춘다."""
    raise NotImplementedError
