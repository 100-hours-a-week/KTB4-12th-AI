"""0004 — profile_runs 열 주석 정정 (ai_search → ai_catalog · updated_at 의 역할)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27

동작 변경 없음 — 열 주석(COMMENT ON COLUMN) 두 개만 바꾼다.
  catalog_version_id  0002 가 쓰일 때 카탈로그는 파일이었고 주석은 팀원 스키마 이름(ai_search)을 가리켰다.
                      지금 들어가는 값은 ai_catalog.catalog_versions.id (0003) 다. FK 는 여전히 걸지 않는다(감사 기록).
  updated_at          09-25 접수 단계 중복 판정(intake.decide)과 시작 시 정리(stores.recover_stale_runs)가
                      이 열을 기준으로 "끊긴 RUNNING"을 가른다(RUNNING_STALE_S). 주석에 그 역할을 남긴다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "ai_profile"
TABLE = "profile_runs"

CATALOG_OLD = "검색에 쓴 카탈로그 버전 (ai_search.catalog_versions.id) — 감사용, FK 없음 (§16.1). 파일 카탈로그는 고정 UUID"
CATALOG_NEW = "검색에 쓴 카탈로그 버전 (ai_catalog.catalog_versions.id) — 감사용, FK 없음 (§16.1). 파일 카탈로그는 고정 UUID"
UPDATED_OLD = "상태가 바뀔 때마다 갱신"
UPDATED_NEW = ("저장할 때마다 now(). RUNNING 이 RUNNING_STALE_S 보다 오래되면 끊긴 실행으로 본다 — "
               "접수 단계 중복 판정·시작 시 정리(FAILED)의 기준")


def _set_comments(catalog: str, updated: str, *, catalog_was: str, updated_was: str) -> None:
    op.alter_column(TABLE, "catalog_version_id", schema=SCHEMA,
                    existing_type=pg.UUID(as_uuid=True), existing_nullable=True,
                    comment=catalog, existing_comment=catalog_was)
    op.alter_column(TABLE, "updated_at", schema=SCHEMA,
                    existing_type=sa.DateTime(timezone=True), existing_nullable=False,
                    existing_server_default=sa.text("now()"),
                    comment=updated, existing_comment=updated_was)


def upgrade() -> None:
    _set_comments(CATALOG_NEW, UPDATED_NEW, catalog_was=CATALOG_OLD, updated_was=UPDATED_OLD)


def downgrade() -> None:
    _set_comments(CATALOG_OLD, UPDATED_OLD, catalog_was=CATALOG_NEW, updated_was=UPDATED_NEW)
