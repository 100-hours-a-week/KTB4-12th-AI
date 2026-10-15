"""마이그레이션 0003 · 0008(ai_catalog, 키 = Backend 번호) 과 적재 SQL — 진짜 PostgreSQL. DB 없으면 skip.
실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q

시험용 번호(9,000,000,000 대 — 실제 Backend 번호 1~수천과 겹치지 않음)를 쓰고 끝에 지운다. 활성 버전 포인터는 건드리지 않는다(activate=False)."""

import json

import pytest
import sqlalchemy as sa

from profiling.settings import get_settings
from tools.catalog import load_catalog as lc

SCHEMA = "ai_catalog"
GID, CID = "TEST-GROUP", "TEST-CAT-01"                 # 패키지 안의 수집처 ID(적재할 때 Backend 번호로 바뀐다)
PIDS = ["TEST_SRC:1", "TEST_SRC:2"]
FAKE_BE = 9_000_000_000
CAT_IDS = {GID: FAKE_BE + 1, CID: FAKE_BE + 2}         # 수집처 → Backend 카테고리 번호
PROD_IDS = {PIDS[0]: FAKE_BE + 101, PIDS[1]: FAKE_BE + 102}


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
            c.execute(sa.text(f"delete from {SCHEMA}.products where id >= :lo"), {"lo": FAKE_BE})
            c.execute(sa.text(f"delete from {SCHEMA}.categories where id >= :lo and level = 2"), {"lo": FAKE_BE})
            c.execute(sa.text(f"delete from {SCHEMA}.categories where id >= :lo"), {"lo": FAKE_BE})
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


def _load(engine, pkg=None):
    return lc.load(engine, pkg or _pkg(), PROD_IDS, CAT_IDS, activate=False)


# ---------------------------------------------------------------- 마이그레이션 결과


def test_tables_and_constraints_exist(engine) -> None:
    with engine.begin() as c:
        tables = {r[0] for r in c.execute(sa.text(
            "select table_name from information_schema.tables where table_schema = :s"), {"s": SCHEMA})}
        assert tables == {"catalog_versions", "categories", "products"}
        checks = {r[0] for r in c.execute(sa.text(
            "select constraint_name from information_schema.table_constraints "
            "where table_schema = :s and constraint_type = 'CHECK'"), {"s": SCHEMA})}
        assert {"ck_categories_level", "ck_categories_parent_by_level", "ck_categories_id", "ck_products_type",
                "ck_products_unit_price", "ck_products_id", "ck_products_availability"} <= checks
        cols = {(r[0], r[1]): r[2] for r in c.execute(sa.text(
            "select table_name, column_name, is_nullable from information_schema.columns where table_schema = :s"), {"s": SCHEMA})}
        # 0008: 키는 Backend 번호 — 수집처 키 열은 없고, 수집처 상품 ID 는 키가 아닌 일반 열
        assert cols[("categories", "id")] == "NO" and cols[("products", "id")] == "NO" and cols[("products", "category_id")] == "NO"
        assert ("categories", "source_category_id") not in cols and ("products", "source_category_id") not in cols
        assert ("products", "backend_product_id") not in cols and cols[("products", "source_product_id")] == "YES"
        pks = {r[0]: r[1] for r in c.execute(sa.text(
            "select tc.table_name, kcu.column_name from information_schema.table_constraints tc "
            "join information_schema.key_column_usage kcu on kcu.constraint_name = tc.constraint_name and kcu.table_schema = tc.table_schema "
            "where tc.table_schema = :s and tc.constraint_type = 'PRIMARY KEY' and tc.table_name in ('categories', 'products')"), {"s": SCHEMA})}
        assert pks == {"categories": "id", "products": "id"}


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
        c.execute(sa.text(f"insert into {SCHEMA}.categories (id, parent_id, name, level, product_count, taxonomy_version) "
                          "values (:id, null, '부모없는소분류', 2, 0, 'v')"), {"id": FAKE_BE + 9})


# ---------------------------------------------------------------- 적재


def test_load_inserts_and_is_idempotent(engine, clean) -> None:
    pkg = _pkg()
    first = _load(engine, pkg)
    assert first["products"] == 2 and first["categories"] == 2
    with engine.begin() as c:
        row = c.execute(sa.text(f"select source_product_id, category_id, name, unit_price, attributes, availability, view_count "
                                f"from {SCHEMA}.products where id = :i"), {"i": PROD_IDS[PIDS[0]]}).mappings().one()
        parent = c.execute(sa.text(f"select parent_id from {SCHEMA}.categories where id = :i"), {"i": CAT_IDS[CID]}).scalar_one()
    assert row["source_product_id"] == PIDS[0] and row["category_id"] == CAT_IDS[CID]      # 키 · 소분류는 Backend 번호
    assert parent == CAT_IDS[GID]
    assert row["name"] == "상품0" and row["unit_price"] == 1000 and row["attributes"] == {"k": "v"}
    assert row["availability"] == "unknown" and row["view_count"] is None

    pkg["products"][0]["productName"] = "이름바뀜"
    _load(engine, pkg)                                       # 같은 패키지를 다시 — 상품 수는 그대로, 값은 갱신
    with engine.begin() as c:
        assert c.execute(sa.text(f"select count(*) from {SCHEMA}.products where id >= :lo"), {"lo": FAKE_BE}).scalar_one() == 2
        assert c.execute(sa.text(f"select name from {SCHEMA}.products where id = :i"),
                         {"i": PROD_IDS[PIDS[0]]}).scalar_one() == "이름바뀜"


def test_load_refuses_without_backend_id(engine, clean) -> None:
    """Backend 번호가 없는 상품 · 카테고리가 하나라도 있으면 적재하지 않는다 — 키를 만들 수 없다(0008)."""
    with pytest.raises(lc.PackageError, match="Backend 번호가 없는 상품 1건"):
        lc.load(engine, _pkg(), {PIDS[0]: FAKE_BE + 101}, CAT_IDS, activate=False)
    with pytest.raises(lc.PackageError, match="Backend 번호가 없는 카테고리 1건"):
        lc.load(engine, _pkg(), PROD_IDS, {CID: FAKE_BE + 2}, activate=False)
    with engine.begin() as c:
        assert c.execute(sa.text(f"select count(*) from {SCHEMA}.products where id >= :lo"), {"lo": FAKE_BE}).scalar_one() == 0


def test_load_refuses_duplicate_backend_id(engine, clean) -> None:
    """두 상품이 같은 Backend 번호를 받으면 적재 전에 막는다(DB PK 까지 가기 전에)."""
    with pytest.raises(lc.PackageError, match="번호가 겹친다"):
        lc.load(engine, _pkg(), {PIDS[0]: FAKE_BE + 101, PIDS[1]: FAKE_BE + 101}, CAT_IDS, activate=False)


def test_load_keeps_availability_and_views(engine, clean) -> None:
    """재적재가 재고 상태 · 조회수를 지우면 안 된다 — 그 두 열은 --metrics(Backend 회신)가 채운다."""
    _load(engine)
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.products set availability = 'available', view_count = 42 where id = :i"),
                  {"i": PROD_IDS[PIDS[0]]})
    _load(engine)
    with engine.begin() as c:
        row = c.execute(sa.text(f"select availability, view_count from {SCHEMA}.products where id = :i"),
                        {"i": PROD_IDS[PIDS[0]]}).mappings().one()
    assert row["availability"] == "available" and row["view_count"] == 42


# ---------------------------------------------------------------- 재고 · 조회수 회신


def test_apply_metrics_fills_only_stock_and_views(engine, clean, tmp_path) -> None:
    """재고·조회수 회신은 그 두 열만 바꾼다 — 이름·가격은 그대로. Backend 번호(productId)로도, 옛 형식(sourceProductId)으로도 찾는다."""
    _load(engine)
    path = tmp_path / "metrics.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"productId": PROD_IDS[PIDS[0]], "availability": "available", "viewCount": 47753},
        {"sourceProductId": PIDS[1], "availability": "unavailable", "viewCount": 0},
        {"productId": FAKE_BE + 999, "availability": "available", "viewCount": 1},   # 우리 표에 없는 것
    ]), encoding="utf-8")

    r = lc.apply_metrics(engine, path)
    assert r == {"updated": 2, "skipped": 0, "missing": 1}
    with engine.begin() as c:
        rows = {x["id"]: x for x in c.execute(sa.text(
            f"select id, availability, view_count, name, unit_price from {SCHEMA}.products where id >= :lo"), {"lo": FAKE_BE}).mappings()}
    a, b = rows[PROD_IDS[PIDS[0]]], rows[PROD_IDS[PIDS[1]]]
    assert (a["availability"], a["view_count"]) == ("available", 47753)
    assert (b["availability"], b["view_count"]) == ("unavailable", 0)
    assert a["name"] == "상품0" and a["unit_price"] == 1000


def test_apply_metrics_rejects_bad_availability(engine, clean, tmp_path) -> None:
    """3값 밖은 파일 단계에서 막는다 (DB CHECK 까지 가기 전에)."""
    _load(engine)
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"productId": PROD_IDS[PIDS[0]], "availability": "품절", "viewCount": 1}), encoding="utf-8")
    with pytest.raises(lc.PackageError, match="3값이 아니다"):
        lc.apply_metrics(engine, path)
