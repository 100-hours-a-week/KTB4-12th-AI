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
    base = {"SERVICE_TOKEN": "a-real-looking-token", "DATABASE_URL": "postgresql+psycopg://ai_app:S3cret@db:5432/ai_chat",
            "BACKEND_BASE_URL": "http://be.internal:8080", "CATALOG_SOURCE": "db"}
    return Settings(**{**base, **kw})


def test_strict_problems_pass_with_server_values() -> None:
    assert main.strict_problems(_settings()) == []


def test_strict_problems_catch_public_defaults() -> None:
    probs = main.strict_problems(_settings(SERVICE_TOKEN="dev-token",
                                           DATABASE_URL="postgresql+psycopg://ai_app:ai_app_password@db:5432/ai_chat",
                                           BACKEND_BASE_URL="http://localhost:8081", CATALOG_SOURCE="file"))
    assert len(probs) == 4
    assert not any("dev-token" in p or "ai_app_password" in p for p in probs)   # 값 자체는 메시지에 넣지 않는다
