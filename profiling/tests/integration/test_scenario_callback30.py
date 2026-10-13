"""프로파일링 코어 30개 상품 콜백 검증 — tools/scenario_callback30 의 시나리오(가상 수신자 엑셀 전원 — 정상 10 + 비정상 12 · 10-13 — × 두 모드)를 시험으로 돌린다.

앱 전체(7.6 → 분석 → 저장 → 진짜 HttpBackendPort 의 7.7)를 서버 없이 탄다. DB 카탈로그(ai_catalog)가 필요하고, 없으면 skip.
결과 엑셀·JSON 은 pytest 임시 폴더에 쓴다(저장소 out/ 을 더럽히지 않음). 실패하면 실패한 검사 이름을 모두 보여 준다.
"""

import os

import pytest
import sqlalchemy as sa

from profiling.settings import get_settings


@pytest.fixture(scope="module")
def db_catalog_ready():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            active = c.execute(sa.text("select count(*) from ai_catalog.catalog_versions where is_active")).scalar()
            products = c.execute(sa.text("select count(*) from ai_catalog.products where backend_product_id is not null")).scalar()
    except sa.exc.SQLAlchemyError as e:
        pytest.skip(f"PostgreSQL·카탈로그를 읽을 수 없음: {type(e).__name__}")
    finally:
        eng.dispose()
    if not active or not products:
        pytest.skip("활성 DB 카탈로그(BE 번호)가 없음 — load_catalog · import_be_ids 먼저")


def test_scenarios_all_checks_pass(db_catalog_ready, tmp_path) -> None:
    from tools.scenario_callback30 import run as sc

    saved = dict(os.environ)                           # run_mode 가 환경변수를 바꾼다 — 다른 시험으로 새지 않게 통째로 되돌린다
    try:
        res = sc.run(out_dir=tmp_path, quiet=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()
    failed = [f"{no}/{mode} {kind} · {name} — {detail}" for (no, mode), ch in res["checks"].items() for kind, name, ok, detail in ch
              if not ok and sc.blocking((kind, name, ok, detail))]      # 관찰 등급은 보고만(품질 비교용)
    assert not failed, "\n".join(failed)
    users = sc.load_scenarios(sc.DEFAULT_SCENARIOS)   # 10-13 비정상 사용자 U11~U22 추가로 22명 — 엑셀 인원에서 센다
    assert len(res["results"]) == len(users) * len(sc.MODES) and res["xlsx"].exists() and res["json"].exists()
