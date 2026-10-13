"""0007 — 앱 전용 DB 계정 ai_app: ai_profile 읽기 · 쓰기, ai_catalog 읽기만, 표 구조 변경 없음

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13

근거: 10-13 DB 검토 — 앱이 DB 소유자 계정(로컬 compose 는 superuser)으로 접속해 표를 지우거나 바꿀 수 있었다. 0006(DR-039)부터
사람마다 정리본 글을 보관하므로 앱 계정의 권한을 하는 일만큼으로 줄인다. 마이그레이션 · 카탈로그 적재(tools/catalog) · 시험은 소유자 계정 그대로.

  역할       ai_app — 클러스터 공용 이름이라 이미 있으면 만들지 않는다.
             로그인 비밀번호는 코드에 없다: 마이그레이션을 돌릴 때 PROFILING_APP_DB_PASSWORD 가 있으면 LOGIN + 그 비밀번호(compose 가 넘긴다),
             없으면 NOLOGIN 으로 만들고 운영자가 소유자 계정으로 한 번 `ALTER ROLE ai_app LOGIN PASSWORD '…'`.
  권한       ai_profile              USAGE · 표 SELECT · INSERT · UPDATE · DELETE (저장 문장의 리뷰 삭제가 DELETE 를 쓴다)
             ai_catalog              USAGE · 표 SELECT 만 (적재 · 번호 반영은 load_catalog.py — 소유자)
             public.alembic_version  SELECT (기동 · /health 가 마이그레이션 번호를 읽는다)
             앞으로 이 마이그레이션을 돌리는 계정이 두 스키마에 만드는 표에도 같은 권한(ALTER DEFAULT PRIVILEGES)
  안 주는 것 스키마 CREATE · TRUNCATE · REFERENCES · TRIGGER — 표 구조는 마이그레이션만 바꾼다

되돌리면(downgrade) 권한만 거둔다. 역할은 다른 DB 가 쓸 수 있어 지우지 않는다(필요하면 손으로 DROP ROLE ai_app).
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

ROLE = "ai_app"
PASSWORD_ENV = "PROFILING_APP_DB_PASSWORD"


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
            END IF;
        END $$""")
    password = os.environ.get(PASSWORD_ENV)
    if password and not context.is_offline_mode():
        # 비밀번호는 바인드 값으로만 넘긴다 — 트랜잭션 안의 설정값(set_config ..., true)에 담아 format(%L)로 인용
        op.get_bind().execute(sa.text("select set_config('profiling.app_db_password', :pw, true)"), {"pw": password})
        op.execute(f"DO $$ BEGIN EXECUTE format('ALTER ROLE {ROLE} LOGIN PASSWORD %L', "
                   "current_setting('profiling.app_db_password')); END $$")
    op.execute(f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database()); END $$")
    op.execute(f"GRANT USAGE ON SCHEMA ai_profile, ai_catalog TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ai_profile TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA ai_catalog TO {ROLE}")
    op.execute(f"GRANT SELECT ON public.alembic_version TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA ai_profile GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA ai_catalog GRANT SELECT ON TABLES TO {ROLE}")


def downgrade() -> None:
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA ai_catalog REVOKE SELECT ON TABLES FROM {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA ai_profile REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE}")
    op.execute(f"REVOKE ALL ON public.alembic_version FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA ai_catalog FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA ai_profile FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA ai_profile, ai_catalog FROM {ROLE}")
    op.execute(f"DO $$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {ROLE}', current_database()); END $$")
