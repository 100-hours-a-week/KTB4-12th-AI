"""catalog.DbCatalogReader — 진짜 PostgreSQL(ai_catalog)에서 활성 버전 한 벌을 읽는지. DB 없거나 적재 전이면 skip.

준비: docker compose up -d && uv run alembic upgrade head
      uv run python -m tools.catalog.load_catalog --package ~/Downloads/product-catalog-20260922-v1

실제 적재분을 읽는 시험이라 **행을 바꾸는 시험은 반드시 되돌린다**(try/finally). 활성 버전 포인터도 원래대로 돌려놓는다.
"""

import pytest
import sqlalchemy as sa

from profiling.catalog import DbCatalogReader
from profiling.ports import CatalogReader, NoActiveCatalog
from profiling.settings import get_settings

SCHEMA = "ai_catalog"


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    with eng.connect() as c:
        if not c.execute(sa.text(f"select count(*) from {SCHEMA}.catalog_versions where is_active")).scalar_one():
            pytest.skip("활성 카탈로그가 없음 — load_catalog.py 로 적재 후 실행")
    yield eng
    eng.dispose()


@pytest.fixture
def active(engine):
    """활성 버전의 (id, package_id). 상품 수도 같이."""
    with engine.connect() as c:
        row = c.execute(sa.text(f"select id, package_id from {SCHEMA}.catalog_versions where is_active")).one()
        n = c.execute(sa.text(f"select count(*) from {SCHEMA}.products where package_id = :p"), {"p": row[1]}).scalar_one()
    return row[0], row[1], n


def test_reads_active_version_and_maps_fields(engine, active) -> None:
    version_id, _, n = active
    cat = DbCatalogReader(engine)
    assert isinstance(cat, CatalogReader)                     # ports 모양
    v, products = cat.active()
    assert v == version_id and len(products) == n             # 활성 버전 id 가 그대로 catalog_version_id 가 된다
    assert [p.productId for p in products] == sorted(p.productId for p in products)   # 7.9 와 같은 순서

    p = products[0]
    with engine.connect() as c:
        row = c.execute(sa.text(
            f"select p.name, p.unit_price, p.availability, c.name as cname from {SCHEMA}.products p "
            f"join {SCHEMA}.categories c on c.source_category_id = p.source_category_id "
            # Backend 번호가 채워졌으면 그것이 상품 번호, 아직이면 수집처 ID 의 숫자부(임시)
            "where coalesce(p.backend_product_id, split_part(p.source_product_id, ':', 2)::bigint) = :pid"),
            {"pid": p.productId}).mappings().one()
    assert (p.name, p.price, p.availability, p.categoryName) == (row["name"], row["unit_price"], row["availability"], row["cname"])
    assert cat.by_id(p.productId) is p and cat.by_id(-1) is None


def test_same_version_is_not_reread(engine) -> None:
    """버전이 그대로면 들고 있던 목록을 그대로 준다 — 요청마다 수천 건을 다시 읽지 않는다."""
    cat = DbCatalogReader(engine)
    _, first = cat.active()
    _, second = cat.active()
    assert second is first


def test_provisional_id_gives_way_to_backend_id(engine, active) -> None:
    """Backend 번호가 있으면 그 번호가 상품 번호. 비어 있는 동안에만 수집처 ID 숫자부(임시)를 쓴다."""
    _, package_id, _ = active
    with engine.connect() as c:
        row = c.execute(sa.text(f"select source_product_id, backend_product_id from {SCHEMA}.products "
                                "where package_id = :p and backend_product_id is not null "
                                "order by source_product_id limit 1"), {"p": package_id}).first()
    if row is None:
        pytest.skip("Backend 번호가 아직 없음 — import_be_ids + load_catalog --id-map 뒤 실행")
    sid, backend_id = row
    provisional = int(sid.split(":")[1])

    cat = DbCatalogReader(engine)
    cat.active()
    assert cat.by_id(backend_id) is not None            # Backend 번호로 찾힌다
    assert cat.by_id(provisional) is None or provisional == backend_id
    assert cat.provisional_ids is False                 # 전건 채워졌으면 표시가 없다

    with engine.begin() as c:                           # 한 건만 비워 임시 번호 경로를 확인
        c.execute(sa.text(f"update {SCHEMA}.products set backend_product_id = null where source_product_id = :s"), {"s": sid})
    try:
        fresh = DbCatalogReader(engine)
        fresh.active()
        assert fresh.by_id(provisional) is not None     # 번호가 없으면 임시 번호로
        assert fresh.by_id(backend_id) is None
        assert fresh.provisional_ids is True            # 한 건이라도 임시면 표시
    finally:
        with engine.begin() as c:
            c.execute(sa.text(f"update {SCHEMA}.products set backend_product_id = :b where source_product_id = :s"),
                      {"b": backend_id, "s": sid})


def test_duplicate_product_number_is_refused(engine, active) -> None:
    """임시 번호와 Backend 번호가 겹치면 다른 상품을 추천하게 된다 — 읽기 자체를 거부한다."""
    _, package_id, _ = active
    with engine.connect() as c:
        row = c.execute(sa.text(f"select source_product_id, source_category_id, "
                                "coalesce(backend_product_id, split_part(source_product_id, ':', 2)::bigint) "
                                f"from {SCHEMA}.products where package_id = :p order by source_product_id limit 1"),
                        {"p": package_id}).one()
    _sid, cid, taken = row
    clash = f"DUPTEST:{taken}"                                 # 숫자부가 기존 Backend 번호와 같다 → 임시 번호가 충돌
    with engine.begin() as c:
        c.execute(sa.text(f"insert into {SCHEMA}.products (source_product_id, name, brand, source_category_id, product_kind, "
                          "product_type, description, unit_price, source_provider, source_product_url, source_image_url, package_id) "
                          "values (:s, '중복시험', 'b', :c, 'k', 'Shipping', 'd', 1000, 'TEST', 'https://x', 'https://x.jpg', :p)"),
                  {"s": clash, "c": cid, "p": package_id})
    try:
        with pytest.raises(NoActiveCatalog, match="겹칩니다"):
            DbCatalogReader(engine).active()
    finally:
        with engine.begin() as c:
            c.execute(sa.text(f"delete from {SCHEMA}.products where source_product_id = :s"), {"s": clash})


def test_no_active_version_raises(engine, active) -> None:
    """활성 버전이 없으면 NoActiveCatalog — main.lifespan 이 잡아서 7.6 이 503 을 낸다."""
    version_id, _, _ = active
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = false where id = :v"), {"v": version_id})
    try:
        with pytest.raises(NoActiveCatalog, match="활성 버전이 없습니다"):
            DbCatalogReader(engine).active()
    finally:
        with engine.begin() as c:
            c.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = true where id = :v"), {"v": version_id})
