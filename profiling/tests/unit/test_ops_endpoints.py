"""운영 엔드포인트 · 기동 검사(10-15) — /healthz(Liveness) · /readyz(Readiness) · /health 의 llm · embedding 칸 · STRICT_STARTUP.

인프라 요구 §4: Liveness 와 Readiness 를 나눈다 · 헬스가 외부 AI 장애와 프로세스 장애를 구분한다. BE 핑 계약(/health 의 두 키)은
test_health_contract.py 가 지킨다 — 여기서는 새 칸이 그 계약을 깨지 않는지도 본다.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from profiling import main, status
from profiling.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


class _Catalog:
    def active(self):
        return "v", [object()] * 3


class _DeadEngine:
    def connect(self):
        raise sa.exc.OperationalError("select 1", {}, Exception("db down"))


class _Supervisor:
    def stats(self) -> dict:
        return {"slots": 1}


def test_expected_migration_is_alembic_head() -> None:
    """새 마이그레이션을 더하고 EXPECTED_MIGRATION 을 안 올리면 여기서 실패한다."""
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [main.EXPECTED_MIGRATION]


def test_migration_ok_accepts_same_or_newer_db() -> None:
    """롤링 배포 — 마이그레이션이 먼저 돌아 DB 가 한 판 높아도 옛 Task 는 준비 상태여야 한다(같음 비교면 그 순간 모두 503)."""
    exp = main.EXPECTED_MIGRATION
    newer = f"{int(exp) + 1:04d}"
    older = f"{int(exp) - 1:04d}"
    assert main.migration_ok(exp) and main.migration_ok(newer)
    assert not main.migration_ok(older)
    assert not main.migration_ok(None) and not main.migration_ok("") and not main.migration_ok("12345")


def test_ready_checks_pass_on_newer_db_and_fail_on_older() -> None:
    class _Conn:
        def __init__(self, v): self.v = v
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execution_options(self, **k): return self
        def execute(self, *a, **k): return SimpleNamespace(scalar=lambda: self.v)

    class _Engine:
        def __init__(self, v): self.v = v
        def connect(self): return _Conn(self.v)

    exp = main.EXPECTED_MIGRATION
    ok = main._ready_checks(SimpleNamespace(catalog=_Catalog(), engine=_Engine(f"{int(exp) + 1:04d}")))
    assert ok == {"catalog": True, "db": True, "migration": True}
    low = main._ready_checks(SimpleNamespace(catalog=_Catalog(), engine=_Engine(f"{int(exp) - 1:04d}")))
    assert low == {"catalog": True, "db": True, "migration": False}


def test_healthz_always_ok_without_db() -> None:
    assert asyncio.run(main.healthz()) == {"status": "ok"}


def test_ready_checks_fail_when_db_down_or_no_catalog() -> None:
    checks = main._ready_checks(SimpleNamespace(catalog=_Catalog(), engine=_DeadEngine()))
    assert checks == {"catalog": True, "db": False, "migration": False}
    checks = main._ready_checks(SimpleNamespace(catalog=main._NoCatalog("없음"), engine=_DeadEngine()))
    assert checks["catalog"] is False


def test_health_has_llm_and_embedding_without_breaking_be_keys() -> None:
    status.LLM.reset()
    status.LLM.configure(requested="openrouter", enabled=False, reason="no_api_key")
    body = main._health_sync(SimpleNamespace(catalog=_Catalog(), engine=_DeadEngine(), store=None, supervisor=_Supervisor()))
    assert isinstance(body["catalog"]["active"], bool) and isinstance(body["store"]["connected"], bool)   # BE 핑 두 키 그대로
    assert body["llm"]["state"] == "off" and body["llm"]["reason"] == "no_api_key"
    assert body["embedding"] == {"enabled": False}


def _settings(**kw) -> Settings:
    base = {"SERVICE_TOKEN": "a-real-looking-token", "DATABASE_URL": "postgresql+psycopg://svc_profiling_x:S3cret@db:5432/ai_chat",
            "BACKEND_BASE_URL": "http://be.internal:8080", "CATALOG_SOURCE": "db"}
    return Settings(**{**base, **kw})


def test_strict_problems_pass_with_server_values() -> None:
    assert main.strict_problems(_settings()) == []


def test_strict_problems_catch_public_defaults() -> None:
    probs = main.strict_problems(_settings(SERVICE_TOKEN="dev-token",
                                           DATABASE_URL="postgresql+psycopg://ai_app:ai_app_password@db:5432/ai_chat",
                                           BACKEND_BASE_URL="http://localhost:8081", CATALOG_SOURCE="file"))
    assert len(probs) == 5                                                          # 토큰 · 계정 이름 · 비밀번호 · BE 주소 · 카탈로그
    assert not any("dev-token" in p or "ai_app_password" in p for p in probs)   # 값 자체는 메시지에 넣지 않는다


def test_strict_problems_refuse_public_app_role_name() -> None:
    """공개된 기본 계정 이름(로컬 기본)으로는 서버가 뜨지 않는다 — 서버는 비공개 이름(PROFILING_APP_DB_ROLE)."""
    probs = main.strict_problems(_settings(DATABASE_URL="postgresql+psycopg://ai_app:S3cret@db:5432/ai_chat"))
    assert len(probs) == 1 and "계정 이름" in probs[0] and "ai_app" not in probs[0]


def test_service_token_check_accepts_only_the_exact_token() -> None:
    """7.6 서비스 토큰 — 맞으면 통과, 없거나 Bearer 가 아니거나 틀리면 401. 비교는 시간 차가 없는 방식(10-17 hmac.compare_digest)."""
    import asyncio

    import pytest
    from fastapi import HTTPException

    from profiling import intake

    s = _settings(SERVICE_TOKEN="right-token-0123456789")
    run = lambda auth: asyncio.run(intake.require_service_token(authorization=auth, settings=s))
    assert run("Bearer right-token-0123456789") is None
    for bad in (None, "right-token-0123456789", "Bearer wrong-token", "Bearer right-token-012345678", "Bearer 한글토큰"):
        with pytest.raises(HTTPException) as e:
            run(bad)
        assert e.value.status_code == 401
    assert asyncio.run(intake.require_service_token(authorization=None, settings=_settings(SERVICE_TOKEN=""))) is None   # 로컬: 토큰 없으면 검사 안 함
