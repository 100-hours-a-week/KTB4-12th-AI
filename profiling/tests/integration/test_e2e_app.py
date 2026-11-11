"""앱 기동 — 앱을 띄워 /health 와 (DB 카탈로그 위) 분석 한 건까지. 저장은 진짜 PostgreSQL.

옛 7.6 → 202 → 7.7 콜백 끝까지 시험은 11-08 HTTP 경로를 RabbitMQ 로 대체하며 지웠다 — 요청 메시지 → 분석 → 결과 발행 끝까지는
tests/integration/test_mq_broker.py(실제 브로커) · test_db_mq_deliveries.py(실제 DB).

저장소가 DB뿐이라 앱 lifespan이 DB에 붙는다 → 단위가 아니라 통합. DB가 안 떠 있으면 skip.
실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q
시험용 수신자 ID(99xxxx)를 쓰고 끝에 지운다 (test_db_stores.py와 같은 규칙)."""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from profiling import pipeline
from profiling.catalog import FILE_CATALOG_VERSION_ID
from profiling.settings import get_settings
from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
from profiling.types import DislikedCategory, ProfileRequest, RunStatus

RID = 990_010



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
    DbProfileRunStore(engine).delete_recipient(RID)
    DbRecipientProfileStore(engine).delete(RID)


def test_health_reports_catalog_and_store(engine) -> None:
    """/health — 예시 카탈로그 111건이 올라오고 저장소는 db로 연결돼 있어야 한다 (앱 lifespan이 실제로 DB에 붙는다)."""
    get_settings.cache_clear()
    from profiling.main import app
    with TestClient(app) as client:
        res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["catalog"] == {"active": True, "version": str(FILE_CATALOG_VERSION_ID), "products": 111, "productTags": "none"}
    assert body["store"]["backend"] == "db" and body["store"]["connected"] is True


def test_app_boots_on_db_catalog(engine, cleanup, monkeypatch) -> None:
    """배포 모양 — PROFILING_CATALOG_SOURCE=db 로 띄우면 ai_catalog 의 활성 버전으로 돌아야 한다.

    컨테이너 안에는 카탈로그 파일이 없으므로 이 경로가 배포 기본값이다. 표의 키가 Backend 번호라(0008) 임시 번호 표시는 없다.
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

    try:
        from profiling.main import app
        with TestClient(app) as client:
            health = client.get("/health").json()
            assert health["catalog"]["active"] is True
            assert health["catalog"]["version"] == str(version_id) and health["catalog"]["products"] == n
            assert "provisional_ids" not in health["catalog"]

            # MQ 처리기가 부르는 것과 같은 분석(mq.runtime.build 의 analyze) — 앱이 조립한 부품 그대로
            rq = ProfileRequest(recipient_user_id=RID, source_version=9, gift_preference=None, reviews=[],
                                disliked_categories=[DislikedCategory(category_id=8, category_name="유아·키즈")])   # 입력이 있어야 30개(전부 비면 0개 — 결정 C)
            out = pipeline.profile(rq, catalog=app.state.catalog, store=app.state.store, recipient_store=app.state.recipient_store, pool_size=30)
            assert out.status is RunStatus.RESULT_READY
            saved = app.state.store.get(RID)
            assert saved.status is RunStatus.RESULT_READY and len(saved.search.product_ids) == 30
            assert saved.search.catalog_version_id == version_id      # 파일 고정 UUID 가 아니라 DB 활성 버전
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
