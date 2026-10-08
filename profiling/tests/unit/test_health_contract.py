"""/health 의 BE 핑 계약 (통합 수정점 v0.7 ①, 2026-10-08 실측).

Backend 는 틱마다 토큰 없이 GET /health 를 부르고, 200 이면서 `catalog.active` 와 `store.connected` 가 **둘 다 bool true** 일 때만
7.6 을 보낸다(BE `AiProfilingClient.isHealthy()`). 키 이름이나 타입이 바뀌면 Backend 가 모든 틱을 건너뛴다 — 이 시험이 그것을 막는다.
DB 없이 main._health_sync 를 가짜 state 로 부른다(저장소는 "DB 죽음"으로 둔다).
"""

from types import SimpleNamespace

import sqlalchemy as sa

from profiling.main import _health_sync, _NoCatalog

BE_PING_KEYS = (("catalog", "active"), ("store", "connected"))     # BE 가 보는 두 키 — 바꾸면 BE 코드도 바뀌어야 한다


class _Catalog:
    def __init__(self, n: int) -> None:
        self._products = [object()] * n

    def active(self):
        return "v-test", self._products


class _DeadEngine:
    def connect(self):
        raise sa.exc.OperationalError("select 1", {}, Exception("db down"))


class _Supervisor:
    def stats(self) -> dict:
        return {"slots": 1, "running": 0, "queued": 0}


def _state(catalog) -> SimpleNamespace:
    return SimpleNamespace(catalog=catalog, engine=_DeadEngine(), store=None, supervisor=_Supervisor())


def test_health_has_be_ping_keys_as_bools() -> None:
    body = _health_sync(_state(_Catalog(3)))
    for top, key in BE_PING_KEYS:
        assert isinstance(body[top][key], bool), f"{top}.{key} 는 bool 이어야 한다 (BE 핑 계약)"
    assert body["catalog"]["active"] is True
    assert body["store"]["connected"] is False          # DB 가 죽었으면 false → BE 가 틱을 건너뛴다


def test_health_reports_inactive_catalog_as_false() -> None:
    body = _health_sync(_state(_NoCatalog("없음")))
    assert body["catalog"]["active"] is False and "reason" in body["catalog"]
