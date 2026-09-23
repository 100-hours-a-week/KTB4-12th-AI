"""마이그레이션 0003(ai_catalog) 과 적재 SQL — 진짜 PostgreSQL. DB 없으면 skip.
실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q

시험용 ID(TEST-… · TEST_SRC:…)를 쓰고 끝에 지운다. 활성 버전 포인터는 건드리지 않는다(activate=False)."""

import json

import pytest
import sqlalchemy as sa

from profiling.settings import get_settings
from tools.catalog import load_catalog as lc

SCHEMA = "ai_catalog"
GID, CID = "TEST-GROUP", "TEST-CAT-01"
PIDS = ["TEST_SRC:1", "TEST_SRC:2"]


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
def clean(engine):
    def _wipe():
        with engine.begin() as c:
            c.execute(sa.text(f"delete from {SCHEMA}.products where source_product_id like 'TEST_SRC:%'"))
            c.execute(sa.text(f"delete from {SCHEMA}.categories where source_category_id like 'TEST-%'"))
            c.execute(sa.text(f"delete from {SCHEMA}.catalog_versions where package_id = 'test-pkg'"))
    _wipe()
    yield
    _wipe()


def _pkg(products=2):
    cats = [{"sourceCategoryId": GID, "parentSourceCategoryId": None, "name": "시험대분류", "level": 1, "productCount": products},
            {"sourceCategoryId": CID, "parentSourceCategoryId": GID, "name": "시험소분류", "level": 2, "productCount": products}]
    prods = [{"sourceProductId": PIDS[i], "productName": f"상품{i}", "brandName": "브랜드", "sourceCategoryId": CID,
              "productKind": "종류", "productType": "Shipping", "description": "설명", "attributes": {"k": "v"},
              "unitPrice": 1000 * (i + 1), "listPrice": None, "currency": "KRW", "stockQuantity": None,
              "source": {"provider": "KAKAO_GIFT", "productUrl": "https://x", "imageUrl": "https://x.jpg"},
              "images": [{"assetId": "f" * 24, "displayOrder": 1}]} for i in range(products)]
    return {"summary": {"packageId": "test-pkg", "productCount": products, "sourceFileSha256": {"a": "b"}},
            "taxonomy_version": "2026-09-15.final57", "categories": cats, "products": prods}


# ---------------------------------------------------------------- 마이그레이션 결과


def test_tables_and_constraints_exist(engine) -> None:
    with engine.begin() as c:
        tables = {r[0] for r in c.execute(sa.text(
            "select table_name from information_schema.tables where table_schema = :s"), {"s": SCHEMA})}
        assert tables == {"catalog_versions", "categories", "products"}
        checks = {r[0] for r in c.execute(sa.text(
            "select constraint_name from information_schema.table_constraints "
            "where table_schema = :s and constraint_type = 'CHECK'"), {"s": SCHEMA})}
        assert {"ck_categories_level", "ck_categories_parent_by_level", "ck_products_type",
                "ck_products_unit_price", "ck_products_backend_id"} <= checks
        # Backend ID 는 아직 없을 수 있어야 한다
        assert c.execute(sa.text(
            "select is_nullable from information_schema.columns "
            "where table_schema = :s and table_name = 'products' and column_name = 'backend_product_id'"),
            {"s": SCHEMA}).scalar_one() == "YES"


def test_only_one_active_version(engine, clean) -> None:
    """부분 유니크 인덱스 — 활성 버전은 언제나 최대 한 개."""
    with engine.begin() as c:
        assert c.execute(sa.text(f"select count(*) from {SCHEMA}.catalog_versions where is_active")).scalar_one() <= 1
        idx = c.execute(sa.text("select indexdef from pg_indexes where schemaname = :s and indexname = :n"),
                        {"s": SCHEMA, "n": "one_active_catalog_version"}).scalar_one()
        assert "UNIQUE" in idx and "is_active" in idx
        for _ in range(2):                                   # 비활성 두 행은 얼마든지 들어간다
            c.execute(sa.text(f"insert into {SCHEMA}.catalog_versions "
                              "(package_id, taxonomy_version, product_count, category_count, is_active) "
                              "values ('test-pkg', 'v', 0, 0, false)"))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as c:   # 둘을 동시에 활성으로 = 거부
        c.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = true where package_id = 'test-pkg'"))


def test_leaf_must_have_parent(engine, clean) -> None:
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as c:
        c.execute(sa.text(f"insert into {SCHEMA}.categories "
                          "(source_category_id, parent_source_category_id, name, level, product_count, taxonomy_version) "
                          "values ('TEST-CAT-X', null, '부모없는소분류', 2, 0, 'v')"))


# ---------------------------------------------------------------- 적재


def test_load_inserts_and_is_idempotent(engine, clean) -> None:
    pkg = _pkg()
    first = lc.load(engine, pkg, activate=False)
    assert first["products"] == 2 and first["categories"] == 2
    with engine.begin() as c:
        row = c.execute(sa.text(f"select name, brand, unit_price, attributes, available, view_count, backend_product_id "
                                f"from {SCHEMA}.products where source_product_id = :p"), {"p": PIDS[0]}).mappings().one()
    assert row["name"] == "상품0" and row["unit_price"] == 1000 and row["attributes"] == {"k": "v"}
    assert row["available"] is None and row["view_count"] is None and row["backend_product_id"] is None

    pkg["products"][0]["productName"] = "이름바뀜"
    lc.load(engine, pkg, activate=False)                     # 같은 패키지를 다시 — 상품 수는 그대로, 값은 갱신
    with engine.begin() as c:
        assert c.execute(sa.text(f"select count(*) from {SCHEMA}.products where source_product_id like 'TEST_SRC:%'")).scalar_one() == 2
        assert c.execute(sa.text(f"select name from {SCHEMA}.products where source_product_id = :p"),
                         {"p": PIDS[0]}).scalar_one() == "이름바뀜"


def test_load_keeps_backend_id_and_availability(engine, clean) -> None:
    """재적재가 Backend ID·재고를 지우면 안 된다 — 그 두 열은 다른 경로(회신·7.9)로 채운다."""
    lc.load(engine, _pkg(), activate=False)
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.products set backend_product_id = 777, available = true, view_count = 42 "
                          "where source_product_id = :p"), {"p": PIDS[0]})
    lc.load(engine, _pkg(), activate=False)
    with engine.begin() as c:
        row = c.execute(sa.text(f"select backend_product_id, available, view_count from {SCHEMA}.products "
                                "where source_product_id = :p"), {"p": PIDS[0]}).mappings().one()
    assert row["backend_product_id"] == 777 and row["available"] is True and row["view_count"] == 42


# ---------------------------------------------------------------- Backend ID 회신


def test_apply_id_map_fills_and_reports(engine, clean, tmp_path) -> None:
    lc.load(engine, _pkg(), activate=False)
    path = tmp_path / "product-id-map.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in [
        {"sourceProductId": PIDS[0], "backendProductId": 101},
        {"sourceProductId": PIDS[1], "backendProductId": None},        # 아직 안 준 것 → 건너뜀
        {"sourceProductId": "TEST_SRC:없음", "backendProductId": 999},   # 우리 표에 없는 것
    ]), encoding="utf-8")

    r = lc.apply_id_map(engine, path, kind="product")
    assert r == {"updated": 1, "skipped": 1, "missing": 1}
    with engine.begin() as c:
        assert c.execute(sa.text(f"select backend_product_id from {SCHEMA}.products where source_product_id = :p"),
                         {"p": PIDS[0]}).scalar_one() == 101
        assert c.execute(sa.text(f"select backend_product_id from {SCHEMA}.products where source_product_id = :p"),
                         {"p": PIDS[1]}).scalar_one() is None


def test_backend_product_id_is_unique(engine, clean) -> None:
    """두 상품이 같은 Backend ID 를 가질 수 없다 — 회신 파일이 잘못돼도 DB가 막는다."""
    lc.load(engine, _pkg(), activate=False)
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.products set backend_product_id = 555 where source_product_id = :p"), {"p": PIDS[0]})
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.products set backend_product_id = 555 where source_product_id = :p"), {"p": PIDS[1]})
