"""0005 — 리뷰 신호 보관: recipient_profiles 열 둘 · recipient_reviews 표 (원문은 저장하지 않는다)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

근거: v2 구현 가이드 3단계(2026-10-08) · 저장 설계(2026-10-07) · **10-11 결정 B — 원문 대신 지문(해시)만**.
  위키 데이터-컨텍스트 보강 설계: 7.6 입력(취향 문장·비선호·리뷰)은 "그 실행에서만 쓰고 저장하지 않습니다". 원문 정본은 Backend.
  그래서 리뷰 글·취향 문장 원문 열을 두지 않는다. 리뷰 단위 캐시(안 바뀐 리뷰는 LLM 재추출 없음)는 review_hash 로 판정한다.

  recipient_profiles  + gift_preference_hash  취향 문장 지문 (preference_hash, 없으면 NULL)
                      + category_scores       소분류별 별점 누적 [{category_id, n, score_sum}] — 편향은 읽을 때 계산
  recipient_reviews   수신자 한 사람의 리뷰 ≤10, 상품당 한 행 (PK 수신자·상품). 분석마다 행 전체 교체(스냅샷, 4단계 stores)

미룬 열(10-08 결정): extraction · prompt_version · vocab_version 은 LLM 단계에서 0006.
마이그레이션은 이 브랜치(feat/v2-profiling-init)에만 — 다른 브랜치에 필요해지면 0006 (alembic 은 머리가 둘이면 멈춘다).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "ai_profile"


def upgrade() -> None:
    # 1) 프로필 행에 열 둘 — ADD COLUMN 은 기존 열 뒤에 붙는다
    op.add_column(
        "recipient_profiles",
        sa.Column("gift_preference_hash", sa.CHAR(64), nullable=True,
                  comment="7.6 giftPreference 의 지문 sha256(정규화 글) — 원문은 저장하지 않음(위키: 7.6 입력은 저장 안 함). 없거나 공백이면 NULL"),
        schema=SCHEMA,
    )
    op.add_column(
        "recipient_profiles",
        sa.Column("category_scores", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
                  comment="소분류별 별점 누적 [{category_id, n, score_sum}] — 편향 = score_sum ÷ (n + 2), 저장하지 않고 읽을 때 계산"),
        schema=SCHEMA,
    )

    # 2) 리뷰 자식 표 — 프로필 한 행에 리뷰 ≤10 을 담을 수 없어 따로 둔다
    op.create_table(
        "recipient_reviews",
        sa.Column("recipient_user_id", sa.BigInteger, nullable=False, comment="수신자 사용자 ID — recipient_profiles 의 자식"),
        sa.Column("product_id", sa.BigInteger, nullable=False, comment="리뷰한 상품 (Backend 상품 번호, 7.6 reviews[].productId)"),
        sa.Column("rating", sa.SmallInteger, nullable=False, comment="별점 1~5"),
        sa.Column("review_hash", sa.CHAR(64), nullable=False,
                  comment="sha256(상품 · 별점 · 정규화 글) — 리뷰 단위 캐시 키. 같으면 저장된 추출 결과 재사용(0006). 글 원문은 저장 안 함"),
        sa.Column("position", sa.SmallInteger, nullable=False, comment="7.6 reviews 배열에서의 자리 0~9 (0 = 가장 최신). 건너뛴 중복이 있으면 번호가 빔"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("recipient_user_id", "product_id", name="pk_recipient_reviews"),
        sa.ForeignKeyConstraint(["recipient_user_id"], [f"{SCHEMA}.recipient_profiles.recipient_user_id"],
                                name="fk_recipient_reviews_profile", ondelete="CASCADE"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_recipient_reviews_rating"),
        sa.CheckConstraint("position BETWEEN 0 AND 9", name="ck_recipient_reviews_position"),
        schema=SCHEMA,
        comment="수신자의 리뷰 ≤10 (상품당 한 행) — 분석마다 스냅샷 교체. 원문 없이 상품·별점·지문·자리만",
    )


def downgrade() -> None:
    op.drop_table("recipient_reviews", schema=SCHEMA)
    op.drop_column("recipient_profiles", "category_scores", schema=SCHEMA)
    op.drop_column("recipient_profiles", "gift_preference_hash", schema=SCHEMA)
