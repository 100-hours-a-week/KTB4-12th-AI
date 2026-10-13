"""0006 — 챗봇 조회용 프로필 데이터: 가드레일 정리본 · 축이 붙은 선호 태그 · 태그 상태 · 추천 30개 · 판 이름

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13

근거: 10-12 결정 C(AI 가 7.6 입력 최신 한 벌 보관 — 태그 재시도 · 챗봇 조회) + **10-13 결정(DR-039): 원문이 아니라 가드레일 정리본을 저장**
+ **10-13 결정(DR-040): 같은 정보를 두 열에 두지 않는다** — `preferred_tags` 는 이름을 그대로 두고 축 · 출처가 붙은 모양으로 바꾸고(따로 `tags` 열을
  두지 않음), `disliked_tags`(= `disliked_categories` 의 이름만 옮긴 사본)는 지운다.
  정리본 = guard.clean 의 결과(개인정보 · 욕설 가림, 의미 없는 글 · 인젝션 의심 글은 NULL). LLM 에 들어간 글과 같아서 재시도하면 같은 결과가
  나오고, 원문 정본은 Backend 에 있다. **원문 그대로는 어디에도 저장하지 않는다**(지문 gift_preference_hash · review_hash 는 원문 기준 그대로 — 바뀜 판정용).
  챗봇은 이 열들을 조회 API(AI_챗봇_프로필_조회_API 제안서)로 읽는다. 상품은 번호만 — 이름 · 가격은 챗봇이 번호로 찾는다.

  recipient_profiles  ~ preferred_tags         string[] ≤12 → [{axis, tag, source, ref}] — 추천 30개를 고를 때 쓴 태그 그대로(자르지 않음)
                      - disliked_tags          (지움) 비선호는 disliked_categories(7.6 사본, 번호 + 이름) 하나로
                      + gift_preference_clean  취향 문장 정리본(없거나 의미 없으면 NULL)
                      + guard_version          정리 규칙 판(g4 …)
                      + tag_status             태그 단계 결과 ok · empty · bad_json · no_tags_list · fail:<종류> · skip_no_signal · off · skip
                      + prompt_version         <규격>-<해시>@<모델> (태그를 안 뽑았으면 NULL)
                      + validator_version
                      + recommended_product_ids  콜백과 같은 상품 번호 ≤30(순서 = 추천 순위)
                      + catalog_version_id     그 30개를 고른 카탈로그 판
  recipient_reviews   + review_text_clean      리뷰 글 정리본(글 없음 · 의미 없음이면 NULL)
  profile_runs        + tag_status · prompt_version · validator_version · llm_cost_usd — 실행마다(부하 시험 · 장애 추적: '태그 없이 끝난 건' 집계)

옛 행의 글자 태그(0005 까지의 string[])는 축 · 출처를 알 수 없어 비운다 — 운영 데이터가 없고(10-13), 다음 분석이 채운다.
되돌리면 preferred_tags 는 글자만으로, disliked_tags 는 disliked_categories 의 이름으로 다시 채운다.
삭제(회원 탈퇴 · 동의 철회)는 팀 논의 사항(의사결정기록 OPEN-13) — 지금은 프로필 행을 지우면 리뷰 자식 행이 CASCADE 로 함께 지워진다(0005).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = "ai_profile"
PROFILES = f"{SCHEMA}.recipient_profiles"

_PROFILE_COLUMNS = [
    sa.Column("gift_preference_clean", sa.Text, nullable=True,
              comment="취향 문장 가드레일 정리본(개인정보 · 욕설 가림). 없거나 의미 없음 · 인젝션 의심이면 NULL. 원문은 저장하지 않음(DR-039)"),
    sa.Column("guard_version", sa.String(16), nullable=True, comment="정리 규칙 판(guard.GUARD_VERSION, 예 g4)"),
    sa.Column("tag_status", sa.String(40), nullable=True,
              comment="태그 단계 결과: ok · empty(전부 버림) · bad_json · no_tags_list · fail:<종류>(timeout · http_402 …) · skip_no_signal · off(추출기 꺼짐) · skip(입력 없음)"),
    sa.Column("prompt_version", sa.String(80), nullable=True, comment="<태그 규격>-<프롬프트 해시>@<모델> — 태그를 안 뽑았으면 NULL"),
    sa.Column("validator_version", sa.String(32), nullable=True, comment="검증기 판(v1-skip · tag3-v1 · tag4-v1)"),
    sa.Column("recommended_product_ids", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
              comment="추천 상품 번호 ≤30, 순서 = 순위(7.7 콜백과 같음). 이름 · 가격은 번호로 찾는다"),
    sa.Column("catalog_version_id", pg.UUID(as_uuid=True), nullable=True, comment="30개를 고른 카탈로그 판(ai_catalog)"),
]
_PREFERRED_COMMENT = ("검증기 통과 선호 태그 [{axis, tag, source(preference|review_text), ref(pref|r1…)}] — 추천 30개를 고를 때 쓴 태그 그대로(자르지 않음). "
                      "axis 는 태그 규격의 축(tag3: 카테고리 · IP · 브랜드)")
_PREFERRED_COMMENT_0005 = "선호 태그 string[] (종류+특징 병합, ≤12). v1은 []"
_DISLIKED_TAGS_COMMENT_0005 = "비선호 태그 string[] (Backend 명시 비선호 이름이 앞 + 분석 결과, ≤8)"


def upgrade() -> None:
    # 1) 선호 태그 — 이름은 그대로, 모양만 축 · 출처가 붙은 객체로. 옛 글자 태그는 축을 알 수 없어 비운다(운영 데이터 없음)
    op.execute(f"UPDATE {PROFILES} SET preferred_tags = '[]'::jsonb WHERE jsonb_typeof(preferred_tags -> 0) = 'string'")
    op.alter_column("recipient_profiles", "preferred_tags", schema=SCHEMA, existing_type=pg.JSONB, comment=_PREFERRED_COMMENT)
    # 2) 비선호 — disliked_categories(7.6 사본, 번호 + 이름) 하나로. 이름만 옮긴 사본 열과 그 GIN 인덱스를 지운다
    op.drop_index("ix_recipient_profiles_disliked_tags", table_name="recipient_profiles", schema=SCHEMA)
    op.drop_column("recipient_profiles", "disliked_tags", schema=SCHEMA)
    op.alter_column("recipient_profiles", "disliked_categories", schema=SCHEMA, existing_type=pg.JSONB,
                    comment="7.6 의 명시 비선호 [{category_id, category_name}] — 받은 그대로의 사본(분석 결과 아님). 새 번호가 오면 최신으로 덮는다")
    # 3) 챗봇 조회용 열
    for col in _PROFILE_COLUMNS:
        op.add_column("recipient_profiles", col, schema=SCHEMA)
    op.create_check_constraint("ck_recipient_profiles_recommended_le_30",
                               "recipient_profiles", "jsonb_array_length(recommended_product_ids) <= 30", schema=SCHEMA)
    op.add_column("recipient_reviews",
                  sa.Column("review_text_clean", sa.Text, nullable=True,
                            comment="리뷰 글 가드레일 정리본. 글 없음 · 의미 없음 · 인젝션 의심이면 NULL. 원문은 저장하지 않음(DR-039)"),
                  schema=SCHEMA)
    for col in (sa.Column("tag_status", sa.String(40), nullable=True, comment="이 실행의 태그 단계 결과(recipient_profiles.tag_status 와 같은 값)"),
                sa.Column("prompt_version", sa.String(80), nullable=True, comment="<태그 규격>-<해시>@<모델>"),
                sa.Column("validator_version", sa.String(32), nullable=True, comment="검증기 판"),
                sa.Column("llm_cost_usd", sa.Numeric(12, 8), nullable=True, comment="외부 API 실제 청구액(OpenRouter usage.cost). 로컬 · 끔이면 NULL")):
        op.add_column("profile_runs", col, schema=SCHEMA)

    # 4) 옛 주석 바로잡기 — 원문 대신 정리본을 따로 둔다는 것을 지문 열에도 적는다
    op.alter_column("recipient_profiles", "gift_preference_hash", schema=SCHEMA, existing_type=sa.CHAR(64),
                    comment="7.6 giftPreference 원문의 지문 sha256(정규화 글) — 바뀜 판정용. 원문은 저장하지 않고 정리본은 gift_preference_clean")
    op.alter_column("recipient_reviews", "review_hash", schema=SCHEMA, existing_type=sa.CHAR(64),
                    comment="sha256(상품 · 별점 · 정규화 원문 글) — 리뷰 단위 바뀜 판정 · 캐시 키. 원문은 저장하지 않고 정리본은 review_text_clean")


def downgrade() -> None:
    for name in ("llm_cost_usd", "validator_version", "prompt_version", "tag_status"):
        op.drop_column("profile_runs", name, schema=SCHEMA)
    op.drop_column("recipient_reviews", "review_text_clean", schema=SCHEMA)
    op.drop_constraint("ck_recipient_profiles_recommended_le_30", "recipient_profiles", schema=SCHEMA)
    for col in reversed(_PROFILE_COLUMNS):
        op.drop_column("recipient_profiles", col.name, schema=SCHEMA)
    # 비선호 사본 열을 되살려 disliked_categories 의 이름으로 채운다
    op.add_column("recipient_profiles",
                  sa.Column("disliked_tags", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), comment=_DISLIKED_TAGS_COMMENT_0005),
                  schema=SCHEMA)
    op.execute(f"""UPDATE {PROFILES} SET disliked_tags = coalesce(
                       (SELECT jsonb_agg(c -> 'category_name') FROM jsonb_array_elements(disliked_categories) c), '[]'::jsonb)""")
    op.create_index("ix_recipient_profiles_disliked_tags", "recipient_profiles", ["disliked_tags"], schema=SCHEMA, postgresql_using="gin")
    op.alter_column("recipient_profiles", "disliked_categories", schema=SCHEMA, existing_type=pg.JSONB,
                    comment="7.6에서 받은 명시 비선호 카테고리 [{category_id, category_name}] (내부 이름) — 분석 시점 사본")
    # 선호 태그는 글자만으로(앞 12개)
    op.execute(f"""UPDATE {PROFILES} SET preferred_tags = coalesce(
                       (SELECT jsonb_agg(t.e -> 'tag' ORDER BY t.i) FROM (SELECT e, i FROM jsonb_array_elements(preferred_tags) WITH ORDINALITY AS x(e, i)
                                                                          ORDER BY i LIMIT 12) t), '[]'::jsonb)
                   WHERE jsonb_typeof(preferred_tags -> 0) = 'object'""")
    op.alter_column("recipient_profiles", "preferred_tags", schema=SCHEMA, existing_type=pg.JSONB, comment=_PREFERRED_COMMENT_0005)
    op.alter_column("recipient_profiles", "gift_preference_hash", schema=SCHEMA, existing_type=sa.CHAR(64),
                    comment="7.6 giftPreference 의 지문 sha256(정규화 글) — 원문은 저장하지 않음(위키: 7.6 입력은 저장 안 함). 없거나 공백이면 NULL")
    op.alter_column("recipient_reviews", "review_hash", schema=SCHEMA, existing_type=sa.CHAR(64),
                    comment="sha256(상품 · 별점 · 정규화 글) — 리뷰 단위 캐시 키. 같으면 저장된 추출 결과 재사용(0006). 글 원문은 저장 안 함")
