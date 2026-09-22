"""0002 — profile_runs (실행 기록: 접수·결과·전달의 감사 기록)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22

근거: 3단계 구현 상세 §10.2 (수명과 멱등성) · §16.1 테이블 목록 · §16.2 unique_profile_source · §16.4 (콜백 전에 payload·hash·RESULT_READY 커밋).
작업 큐가 아니다 — 재시작 시 RUNNING은 실패 기록, RESULT_READY는 미전달로 남겨 재요청 때 같은 payload를 재전송하기 위한 기록.
실행 1건당 1행. (recipient_user_id, source_version)은 유니크 — 같은 버전 중복 접수는 새 분석 없이 기존 행을 본다.

`recipient_profiles.profile_run_id`(FK → 이 테이블)는 다음 리비전에서 추가한다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "ai_profile"
STATUSES = ("RUNNING", "RESULT_READY", "DELIVERED", "SUPERSEDED", "FAILED")     # profile.types.RunStatus 와 같은 값


def upgrade() -> None:
    op.create_table(
        "profile_runs",
        # PG enum 타입 대신 text + CHECK — 상태를 추가·삭제할 때 ALTER TYPE 없이 제약만 바꾸면 된다
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), comment="실행 ID (앱이 주거나 DB가 생성)"),
        sa.Column("recipient_user_id", sa.BigInteger, nullable=False, comment="수신자 사용자 ID (Backend 정본)"),
        sa.Column("source_version", sa.BigInteger, nullable=False, comment="7.6 sourceVersion — 논리 키의 절반"),
        sa.Column("input_hash", sa.Text, nullable=False, comment="정규화한 7.6 본문 해시. 같은 키·다른 입력 감지용"),
        sa.Column("status", sa.Text, nullable=False, comment="RUNNING → RESULT_READY → DELIVERED | SUPERSEDED · 실패는 FAILED"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1"), comment="분석 시도 횟수 (실패 재실행 허용 시 증가)"),
        sa.Column("catalog_version_id", pg.UUID(as_uuid=True), nullable=True,
                  comment="검색에 쓴 카탈로그 버전 (ai_search.catalog_versions.id) — 감사용, FK 없음 (§16.1). 파일 카탈로그는 고정 UUID"),
        sa.Column("callback_payload", pg.JSONB, nullable=True, comment="7.7에 보낼 본문. RESULT_READY 이전에는 NULL, 이후 재전송은 이 값을 그대로"),
        sa.Column("callback_hash", sa.Text, nullable=True, comment="callback_payload 해시 — 재전송 시 같은 결과인지 확인"),
        sa.Column("callback_attempts", sa.Integer, nullable=False, server_default=sa.text("0"), comment="콜백 시도 횟수 (최초 1 + 재시도 ≤3)"),
        sa.Column("error", pg.JSONB, nullable=True, comment="실패 사유 {code, message, …}. FAILED 외에는 NULL"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"), comment="접수 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"), comment="상태가 바뀔 때마다 갱신"),
        sa.CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", name="ck_profile_runs_status"),
        sa.CheckConstraint("attempt >= 1", name="ck_profile_runs_attempt"),
        sa.CheckConstraint("callback_attempts >= 0", name="ck_profile_runs_callback_attempts"),
        # §16.4 "payload·hash·RESULT_READY를 먼저 커밋한 뒤 전송" — 결과 없이 결과 상태가 되는 행을 DB가 막는다
        sa.CheckConstraint(
            "status NOT IN ('RESULT_READY', 'DELIVERED', 'SUPERSEDED') OR (callback_payload IS NOT NULL AND callback_hash IS NOT NULL)",
            name="ck_profile_runs_result_has_payload",
        ),
        schema=SCHEMA,
        comment="프로파일링 실행 기록(감사·재전송). 작업 큐가 아님 — polling 대상 아님",
    )
    # §16.2 — 같은 수신자·같은 원본 버전은 한 실행만 (중복 접수는 기존 행을 본다)
    op.create_index("unique_profile_source", "profile_runs", ["recipient_user_id", "source_version"], unique=True, schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("unique_profile_source", table_name="profile_runs", schema=SCHEMA)
    op.drop_table("profile_runs", schema=SCHEMA)
