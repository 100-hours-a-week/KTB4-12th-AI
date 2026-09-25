"""끝에서 끝 — 앱을 띄워 7.6 → 202 → (백그라운드) profile → 7.7 콜백까지. HTTP 콜백만 가짜가 받고, 저장은 진짜 PostgreSQL.

저장소가 DB뿐이라 앱 lifespan이 DB에 붙는다 → 단위가 아니라 통합. DB가 안 떠 있으면 skip.
실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q
시험용 수신자 ID(99xxxx)를 쓰고 끝에 지운다 (test_db_stores.py와 같은 규칙)."""

import json

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from profiling.backend import HttpBackendPort
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


def test_app_boots_on_db_catalog(engine, cleanup, monkeypatch) -> None:
    """배포 모양 — PROFILING_CATALOG_SOURCE=db 로 띄우면 ai_catalog 의 활성 버전으로 돌아야 한다.

    컨테이너 안에는 카탈로그 파일이 없으므로 이 경로가 배포 기본값이다. Backend 번호가 아직 없으면
    /health 가 provisional_ids 로 그 사실을 드러낸다(임시 번호로 7.7을 보내면 Backend에 없는 번호가 된다).
    """
    monkeypatch.setenv("PROFILING_CATALOG_SOURCE", "db")
    get_settings.cache_clear()
    with engine.connect() as c:
        row = c.execute(sa.text("select id, package_id from ai_catalog.catalog_versions where is_active")).first()
    if row is None:
        pytest.skip("활성 카탈로그가 없음 — load_catalog.py 로 적재 후 실행")
    version_id, package_id = row
    with engine.connect() as c:
        n = c.execute(sa.text("select count(*) from ai_catalog.products where package_id = :p"), {"p": package_id}).scalar_one()
        filled = c.execute(sa.text("select count(*) from ai_catalog.products where package_id = :p "
                                   "and backend_product_id is not null"), {"p": package_id}).scalar_one()

    try:
        from profiling.main import app
        with TestClient(app) as client:
            health = client.get("/health").json()
            assert health["catalog"]["active"] is True
            assert health["catalog"]["version"] == str(version_id) and health["catalog"]["products"] == n
            assert health["catalog"].get("provisional_ids", False) is (filled < n)

            app.state.backend = RecordingBackend()
            res = client.post("/api/internal/v1/ai/profile/extract-and-pool",
                              json={"recipientUserId": RID, "sourceVersion": 9, "dislikedCategories": [],
                                    "giftPreference": None, "reviews": []})
            assert res.status_code == 202
            saved = app.state.store.get(RID)
            assert saved.status is RunStatus.DELIVERED and len(saved.search.product_ids) == 30
            assert saved.search.catalog_version_id == version_id      # 파일 고정 UUID 가 아니라 DB 활성 버전
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_same_version_resend_does_not_reanalyze(engine, cleanup) -> None:
    """Backend 재전송(같은 sourceVersion) — 진짜 DB · **진짜 BackendPort**로.

    가짜 BackendPort를 쓰면 to_callback()을 거치지 않아 "재전송이 DELIVERED를 거부한다"는 버그를 놓친다
    (실제로 놓쳤다). 그래서 여기서는 httpx.MockTransport만 끼운 진짜 HttpBackendPort를 쓴다.

      attempt            1 그대로 (분석은 한 번만)
      callback_attempts  2 (7.7은 두 번 나갔다)
      본문               두 번 다 같다
    """
    get_settings.cache_clear()
    from profiling.main import app
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"message": "ok", "data": {"recipientUserId": RID, "sourceVersion": 21,
                                                                  "profileStatus": "COMPLETED"}})

    body = {"recipientUserId": RID, "sourceVersion": 21, "dislikedCategories": [],
            "giftPreference": None, "reviews": []}
    with TestClient(app) as client:
        app.state.backend = HttpBackendPort("http://backend.test", "t", 5.0, transport=httpx.MockTransport(handler))
        assert client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body).status_code == 202
        assert len(sent) == 1 and len(sent[0]["recommendedProductIds"]) == 30

        assert client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body).status_code == 202   # 같은 번호 재전송
        assert len(sent) == 2                                                 # 재전송이 실제로 나갔다
        assert sent[1] == sent[0]                                             # 최초와 완전히 같은 본문

    with engine.connect() as c:
        row = c.execute(sa.text("select attempt, callback_attempts, status, callback_hash "
                                "from ai_profile.profile_runs where recipient_user_id = :r and source_version = 21"),
                        {"r": RID}).mappings().one()
    assert row["attempt"] == 1                       # 분석은 한 번만 돌았다
    assert row["callback_attempts"] == 2             # 콜백은 두 번
    assert row["status"] == "DELIVERED" and row["callback_hash"]
