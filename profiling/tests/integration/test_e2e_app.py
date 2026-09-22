"""끝에서 끝 — 앱을 띄워 7.6 → 202 → (백그라운드) profile → 7.7 콜백까지. HTTP 콜백만 가짜가 받고, 저장은 진짜 PostgreSQL.

저장소가 DB뿐이라 앱 lifespan이 DB에 붙는다 → 단위가 아니라 통합. DB가 안 떠 있으면 skip.
실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q
시험용 수신자 ID(99xxxx)를 쓰고 끝에 지운다 (test_db_stores.py와 같은 규칙)."""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from profiling.catalog import FILE_CATALOG_VERSION_ID
from profiling.settings import get_settings
from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
from profiling.types import CallbackResult, RunStatus

RID = 990_010
RID_UNKNOWN = 990_011


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    yield eng
    eng.dispose()


@pytest.fixture
def cleanup(engine):
    """시험이 남긴 행을 지운다 — adapter가 트랜잭션을 스스로 열어 롤백으로 감쌀 수 없다."""
    yield
    for rid in (RID, RID_UNKNOWN):
        DbProfileRunStore(engine).delete_recipient(rid)
        DbRecipientProfileStore(engine).delete(rid)


class RecordingBackend:
    def __init__(self):
        self.sent = []

    def send_profile_callback(self, outcome):
        self.sent.append(outcome)
        return CallbackResult(status=RunStatus.DELIVERED, http_status=200)


def test_v1_request_end_to_end(engine, cleanup) -> None:
    get_settings.cache_clear()
    from profiling.main import app
    with TestClient(app) as client:                     # lifespan: FileCatalogReader(예시 111건) · Db*Store · HttpBackendPort
        fake = RecordingBackend()
        app.state.backend = fake                        # 진짜 HTTP 대신 기록만 (ports 모양이면 교체 가능)
        body = {"recipientUserId": RID, "sourceVersion": 3,
                "dislikedCategories": [{"categoryId": 802, "categoryName": "출산·육아용품"}],
                "giftPreference": None, "reviews": []}
        res = client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body)
        assert res.status_code == 202
        assert res.json()["data"] == {"recipientUserId": RID, "sourceVersion": 3, "profileStatus": "PENDING"}

        # TestClient는 응답 뒤 BackgroundTasks를 동기로 실행한다 → 여기서 이미 끝나 있음
        assert len(fake.sent) == 1
        outcome = fake.sent[0]
        assert outcome.status is RunStatus.RESULT_READY
        assert len(outcome.search.product_ids) == 30
        assert outcome.validation.disliked_tags == ["출산·육아용품"] and outcome.input_hash
        saved = app.state.store.get(RID)                   # 콜백 뒤 run_and_callback이 결과를 실행 기록에 저장
        assert saved.status is RunStatus.DELIVERED and saved.callback_attempts == 1
        assert saved.search.product_ids == outcome.search.product_ids
        prof = app.state.recipient_store.get(RID)          # 수신자 프로필도 같은 실행에서 upsert
        assert prof.source_version == 3 and prof.disliked_tags == ["출산·육아용품"]
        _, products = app.state.catalog.active()
        by_id = {p.productId: p for p in products}
        assert all(by_id[i].categoryName != "출산·육아용품" for i in outcome.search.product_ids)

        bad = client.post("/api/internal/v1/ai/profile/extract-and-pool", json={**body, "reviews": [{"productId": 1, "rating": 9}]})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_REQUEST"
        assert len(fake.sent) == 1                      # 400은 백그라운드로 가지 않음


def test_unknown_fields_are_accepted_and_logged(engine, cleanup, caplog) -> None:
    """계약에 없는 필드 → 400이 아니라 202 + CONTRACT_7_6_UNKNOWN_FIELD 경고 (Backend가 필드를 추가해도 연동 유지)."""
    get_settings.cache_clear()
    from profiling.main import app
    with TestClient(app) as client:
        app.state.backend = RecordingBackend()
        body = {"recipientUserId": RID_UNKNOWN, "sourceVersion": 1,
                "dislikedCategories": [{"categoryId": 802, "categoryName": "출산·육아용품", "weight": 1}],
                "giftPreference": None, "reviews": [], "extraTop": "x"}
        with caplog.at_level("WARNING", logger="profiling.intake"):
            res = client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body)
        assert res.status_code == 202
        assert any("CONTRACT_7_6_UNKNOWN_FIELD" in r.message and "extraTop" in r.message and "weight" in r.message for r in caplog.records)


def test_health_reports_catalog_and_store(engine) -> None:
    """/health — 예시 카탈로그 111건이 올라오고 저장소는 db로 연결돼 있어야 한다 (앱 lifespan이 실제로 DB에 붙는다)."""
    get_settings.cache_clear()
    from profiling.main import app
    with TestClient(app) as client:
        res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["catalog"] == {"active": True, "version": str(FILE_CATALOG_VERSION_ID), "products": 111}
    assert body["store"]["backend"] == "db" and body["store"]["connected"] is True
