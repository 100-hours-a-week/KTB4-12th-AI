"""0001 — ai_profile 스키마 · vector 확장 · recipient_profiles (수신자 프로필: 태그 보관)

Revision ID: 0001
Revises:
Create Date: 2026-09-22

근거: 담당파트 상세설계서 §1.7 (태그는 AI가 보관, 수신자당 한 행, 최신 분석이 덮어씀).
컬럼은 사용자가 정한 5개 + 관리용 시각 2개. 설계서의 나머지 컬럼(axes·recommended_product_ids·catalog_version_id·profile_run_id·
prompt/validator_version)은 필요해지는 시점에 다음 리비전으로 추가한다 — 마이그레이션은 되돌릴 수 있게 작은 단위로.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "ai_profile"


def upgrade() -> None:
    # 1) 스키마·확장 — vector는 v2·v3 임베딩 테이블용. 지금 미리 켜 둔다 (superuser 권한은 compose의 ai_user가 DB owner라 가능)
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2) 수신자 프로필 — 태그 보관 (v1: 비선호만 채움 · v3: 선호·비선호 태그까지)
    op.create_table(
        "recipient_profiles",
        sa.Column("recipient_user_id", sa.BigInteger, primary_key=True, autoincrement=False, comment="수신자 사용자 ID (Backend 정본). 한 사람당 한 행"),
        sa.Column("source_version", sa.BigInteger, nullable=False, comment="분석한 원본 버전 (7.6 sourceVersion). 낮은 버전으로 덮어쓰지 않음"),
        sa.Column("preferred_tags", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
                  comment="선호 태그 string[] (종류+특징 병합, ≤12). v1은 []"),
        sa.Column("disliked_tags", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
                  comment="비선호 태그 string[] (Backend 명시 비선호 이름이 앞 + 분석 결과, ≤8)"),
        sa.Column("disliked_categories", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
                  comment="7.6에서 받은 명시 비선호 카테고리 [{category_id, category_name}] (내부 이름) — 분석 시점 사본"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema=SCHEMA,
        comment="AI가 보관하는 수신자 프로필(태그). Backend에는 상품 ID만 보낸다",
    )
    # JSONB 배열 검색용 (태그로 수신자를 찾을 일이 생기면) — 지금은 비용이 작아 함께 만든다
    op.create_index("ix_recipient_profiles_preferred_tags", "recipient_profiles", ["preferred_tags"], schema=SCHEMA, postgresql_using="gin")
    op.create_index("ix_recipient_profiles_disliked_tags", "recipient_profiles", ["disliked_tags"], schema=SCHEMA, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_recipient_profiles_disliked_tags", table_name="recipient_profiles", schema=SCHEMA)
    op.drop_index("ix_recipient_profiles_preferred_tags", table_name="recipient_profiles", schema=SCHEMA)
    op.drop_table("recipient_profiles", schema=SCHEMA)
    # 확장·스키마는 다른 테이블이 쓸 수 있으므로 내리지 않는다
