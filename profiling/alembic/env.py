import os
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool

from alembic import context

# ---- profiling 설정에서 DB URL을 가져온다 (환경변수 PROFILING_DATABASE_URL 또는 .env). alembic.ini의 값은 자리표시자.
from profiling.settings import get_settings, read_dotenv

# 0007 이 앱 전용 계정 이름 · 비밀번호를 os.environ 에서 읽는다 — 셸에 없으면 같은 .env 에서 채운다(셸 · Task 값이 우선)
for _k, _v in read_dotenv(".env").items():
    if _k in ("PROFILING_APP_DB_ROLE", "PROFILING_APP_DB_PASSWORD") and _v:
        os.environ.setdefault(_k, _v)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
_url = get_settings().DATABASE_URL
if not _url.strip():
    raise RuntimeError("PROFILING_DATABASE_URL 이 없다 — profiling/.env 또는 환경변수로 넣는다")
config.set_main_option("sqlalchemy.url", _url)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None  # ORM 모델을 만들면 여기에 Base.metadata를 연결해 autogenerate를 쓴다

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # 표 잠금 대기 상한(10-13 DB 검토) — ALTER TABLE 이 앱의 긴 쿼리 뒤에서 ACCESS EXCLUSIVE 를 기다리는 동안 그 뒤의 앱 쿼리가
        # 전부 줄을 서서 서비스가 멈춘다. 오래 기다리느니 마이그레이션을 실패시키고 다시 돌린다. 문장 시간 상한은 두지 않는다(데이터 채우기는 길 수 있다)
        connection.execute(sa.text("select set_config('lock_timeout', :v, false)"),
                           {"v": os.environ.get("PROFILING_MIGRATE_LOCK_TIMEOUT", "5s")})
        connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
