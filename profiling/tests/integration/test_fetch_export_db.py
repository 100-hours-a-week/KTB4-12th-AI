"""fetch_export --compare-db — 팀원 검색 테이블 ai_search.products(product_id TEXT)와 Backend export 대조. DB 없으면 skip.
테이블은 시험 안에서 팀원 DDL 그대로 만들고 끝에 지운다 (우리 마이그레이션 범위 밖)."""

import pytest
import sqlalchemy as sa

from profiling.settings import get_settings
from tools.catalog import fetch_export as fx

DDL = """
CREATE SCHEMA IF NOT EXISTS ai_search;
CREATE TABLE IF NOT EXISTS ai_search.products (
    product_id TEXT PRIMARY KEY, name TEXT NOT NULL, brand TEXT NOT NULL, category_id TEXT NOT NULL,
    price_krw INTEGER NOT NULL CHECK (price_krw > 0), description TEXT NOT NULL
)"""


@pytest.fixture
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
def search_table(engine):
    with engine.begin() as c:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
        c.execute(sa.text("delete from ai_search.products"))
        c.execute(sa.text("""insert into ai_search.products values
            ('1', 'n1', 'b', 'CAT-1-1', 100, 'd'), ('2', 'other name', 'b', 'CAT-1-1', 100, 'd'),
            ('kakao:77', 'raw', 'b', 'CAT-1-2', 100, 'd'), ('9', 'n9', 'b', 'CAT-1-2', 100, 'd')"""))
    yield
    with engine.begin() as c:
        c.execute(sa.text("drop table ai_search.products"))
        c.execute(sa.text("drop schema if exists ai_search"))


def _p(pid):
    return {"productId": pid, "name": f"n{pid}", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 10, "available": True, "updatedAt": "2026-09-01T00:00:00Z"}


def test_compare_db_reports_text_ids_and_gaps(engine, search_table) -> None:
    be = fx.check_contract({"data": {"products": [_p(1), _p(2), _p(3)]}}).valid
    d = fx.compare_db(engine, be)
    assert not d.table_missing and d.total == 4
    assert d.non_numeric_ids == ["kakao:77"]                      # Backend BIGINT와 맞출 수 없는 원형 ID
    assert d.only_in_backend == [3] and d.only_in_ai == [9] and d.common == 2
    assert d.name_changed == [(2, "other name", "n2")]
    assert not d.clean


def test_compare_db_table_missing_is_reported(engine) -> None:
    with engine.begin() as c:                                      # 확실히 없는 상태에서
        c.execute(sa.text("drop table if exists ai_search.products"))
    assert fx.compare_db(engine, []).table_missing is True
