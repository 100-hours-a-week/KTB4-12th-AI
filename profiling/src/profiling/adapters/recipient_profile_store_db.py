"""RecipientProfileStore (PostgreSQL) — ai_profile.recipient_profiles. 수신자 프로필: 한 사람당 한 행, 최신 분석이 덮어씀 (담당파트 설계서 §1.7).

ports.RecipientProfileStore 구현. pipeline.profile()이 RESULT_READY를 실행 기록에 커밋한 직후 upsert()를 부르고,
Chat은 대화 시작 때 get()으로 태그를 읽는다(v3). v1 행의 내용은 source_version + disliked_categories 뿐이고 태그 두 열은 [].

upsert(profile)  INSERT … ON CONFLICT (recipient_user_id) DO UPDATE … WHERE 기존.source_version <= 새.source_version
                 — 낮은 버전이 늦게 도착하면 DB가 무시한다(recipient_profile.should_replace와 같은 규칙). 무시되면 warning 로그.
get(rid)         행 → RecipientProfile. 없으면 None.
delete(rid)      사용자 데이터 삭제 요청용. 지웠으면 True.

저장하는 열(마이그레이션 0001): recipient_user_id · source_version · preferred_tags · disliked_tags · disliked_categories · updated_at.
RecipientProfile의 나머지 필드(profile_run_id · axes · recommended_product_ids · catalog_version_id · prompt/validator_version)는
v3 마이그레이션 0003이 열을 추가할 때 함께 저장한다 — 그 전까지는 객체에만 있고 DB에는 없다(get()으로 되돌리면 기본값).

JSONB 안의 키는 내부 이름(snake_case)이다: disliked_categories = [{"category_id": 802, "category_name": "출산·육아용품"}].
DB는 파이썬 쪽 저장소라 HTTP 경계(camelCase)가 아니다 — 변환 함수를 하나 더 만들지 않기 위해 내부 모델을 그대로 넣는다.
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from profiling.profile.recipient_profile import RecipientProfile
from profiling.profile.types import DislikedCategory

log = logging.getLogger(__name__)

TABLE = "ai_profile.recipient_profiles"

_UPSERT = sa.text(f"""
    insert into {TABLE} (recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories)
    values (:rid, :sv, cast(:preferred as jsonb), cast(:disliked as jsonb), cast(:categories as jsonb))
    on conflict (recipient_user_id) do update set
        source_version      = excluded.source_version,
        preferred_tags      = excluded.preferred_tags,
        disliked_tags       = excluded.disliked_tags,
        disliked_categories = excluded.disliked_categories,
        updated_at          = now()
    where {TABLE}.source_version <= excluded.source_version""")

_GET = sa.text(f"""
    select recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories, updated_at
    from {TABLE} where recipient_user_id = :rid""")


class DbRecipientProfileStore:
    """ports.RecipientProfileStore 구현 (PostgreSQL)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- ports.RecipientProfileStore --------------------------------------------

    def upsert(self, profile: RecipientProfile) -> None:
        params = {
            "rid": profile.recipient_user_id, "sv": profile.source_version,
            "preferred": json.dumps(profile.preferred_tags, ensure_ascii=False),
            "disliked": json.dumps(profile.disliked_tags, ensure_ascii=False),
            "categories": json.dumps([c.model_dump() for c in profile.disliked_categories], ensure_ascii=False),
        }
        with self._engine.begin() as conn:
            written = conn.execute(_UPSERT, params).rowcount
        if written == 0:   # WHERE에 걸림 = 기존 행이 더 새 버전 (순서 역전)
            log.warning("recipient_profiles recipient=%s source_version=%s 는 기존 행보다 낮아 무시", profile.recipient_user_id, profile.source_version)
        else:
            log.debug("recipient_profiles recipient=%s source_version=%s 저장 (disliked_categories=%d)",
                      profile.recipient_user_id, profile.source_version, len(profile.disliked_categories))

    def get(self, recipient_user_id: int) -> RecipientProfile | None:
        with self._engine.begin() as conn:
            row = conn.execute(_GET, {"rid": recipient_user_id}).mappings().first()
        if row is None:
            return None
        return RecipientProfile(
            recipient_user_id=row["recipient_user_id"], source_version=row["source_version"],
            preferred_tags=list(row["preferred_tags"]), disliked_tags=list(row["disliked_tags"]),
            disliked_categories=[DislikedCategory(**c) for c in row["disliked_categories"]],
            updated_at=row["updated_at"],
        )

    def delete(self, recipient_user_id: int) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount > 0
