"""profile/recipient_profile — 태그 보관·갱신 규칙. (목 — 함수 채운 뒤 skip 제거)

여기에 쓸 것:
  - test_from_outcome_maps_fields: outcome(validation·search)+rq → 행. preferred/disliked/axes/explicit ids/recommended ids/버전 라벨
  - test_should_replace_by_source_version: 기존 없음→True · 같음→True · 높음→True · 낮음→False
  - test_merge_explicit_dislikes_front_and_dedup: 명시 비선호가 앞, 중복 제거, 상한 8
  - test_cap_tags: 중복 제거·상한
  - test_to_search_hint_shape: Search 인자 이름과 일치 (팀원 합의 후)
  - test_memory_store_upsert_get_delete: isinstance(…, RecipientProfileStore) 모양 · 덮어쓰기 · 삭제
"""

import pytest

from profiling.profile import recipient_profile as rp

pytestmark = pytest.mark.skip(reason="목 — recipient_profile 함수를 채운 뒤 활성화")


def test_from_outcome_maps_fields() -> None:
    raise NotImplementedError


def test_should_replace_by_source_version() -> None:
    raise NotImplementedError


def test_merge_explicit_dislikes_front_and_dedup() -> None:
    raise NotImplementedError


def test_cap_tags() -> None:
    assert rp.cap_tags([], 1) == []


def test_to_search_hint_shape() -> None:
    raise NotImplementedError


def test_memory_store_upsert_get_delete() -> None:
    raise NotImplementedError
