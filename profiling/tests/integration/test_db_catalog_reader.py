"""catalog.DbCatalogReader — 진짜 PostgreSQL(ai_catalog)에서 활성 버전 한 벌을 읽는지. DB 없거나 적재 전이면 skip.

준비: docker compose up -d && uv run alembic upgrade head
      uv run python -m tools.catalog.load_catalog --package ~/Downloads/product-catalog-20260922-v1

실제 적재분을 읽는 시험이라 **행을 바꾸는 시험은 반드시 되돌린다**(try/finally). 활성 버전 포인터도 원래대로 돌려놓는다.
"""

from uuid import UUID

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


# ---------------------------------------------------------------- 버전과 상품이 한 벌인가


def test_version_comes_from_the_same_statement_as_products(engine, active) -> None:
    """폴링이 **틀린 버전**을 줘도, 돌려주는 버전은 상품을 읽은 그 질의의 값이어야 한다.

    이 둘을 따로 읽으면 그 사이에 카탈로그가 교체될 때 "버전은 옛것, 상품은 새것"인 한 벌이 나온다.
    폴링을 가짜로 바꿔 그 상황을 만든다 — 고치기 전에는 가짜 버전이 그대로 새어 나왔다.
    """
    version_id, _, _ = active
    stale = UUID("00000000-0000-0000-0000-0000deadbeef")
    cat = DbCatalogReader(engine)
    cat._active_version = lambda: (stale, "없는-패키지")

    got_version, products = cat.active()
    assert got_version == version_id and got_version != stale
    assert products and all(p.productId > 0 for p in products)


def test_version_and_products_belong_together(engine, active) -> None:
    """돌려준 버전의 패키지에 속한 상품 수와 목록 길이가 같아야 한다 (섞이지 않았다는 뜻)."""
    cat = DbCatalogReader(engine)
    version_id, products = cat.active()
    with engine.connect() as c:
        package = c.execute(sa.text(f"select package_id from {SCHEMA}.catalog_versions where id = :v"),
                            {"v": version_id}).scalar_one()
        n = c.execute(sa.text(f"select count(*) from {SCHEMA}.products where package_id = :p"), {"p": package}).scalar_one()
    assert len(products) == n


def test_parent_category_is_filled_from_db(engine, active) -> None:
    """상품마다 대분류(parentCategoryId·Name)가 categories 부모 조인으로 채워진다 — 비선호(대분류) 제외의 근거."""
    _, products = DbCatalogReader(engine).active()
    assert all(p.parentCategoryId is not None and p.parentCategoryName for p in products)
    assert {p.parentCategoryId for p in products} <= set(range(1, 11))       # Backend 대분류 id 1~10 (09-25 회신)
    p = products[0]
    with engine.connect() as c:
        row = c.execute(sa.text(
            f"select cp.backend_category_id as pid, cp.name as pname from {SCHEMA}.products p "
            f"join {SCHEMA}.categories c on c.source_category_id = p.source_category_id "
            f"join {SCHEMA}.categories cp on cp.source_category_id = c.parent_source_category_id "
            "where coalesce(p.backend_product_id, split_part(p.source_product_id, ':', 2)::bigint) = :pid"),
            {"pid": p.productId}).mappings().one()
    assert (p.parentCategoryId, p.parentCategoryName) == (row["pid"], row["pname"])


# ---------------------------------------------------------------- 폴링 TTL (09-28 왕복 줄이기)


def _count_polls(cat: DbCatalogReader) -> list[int]:
    """cat._active_version 을 감싸 호출 수를 센다 (test_version_comes_from… 과 같은 인스턴스 속성 치환)."""
    calls, real = [0], cat._active_version

    def wrapped():
        calls[0] += 1
        return real()

    cat._active_version = wrapped
    return calls


def test_poll_ttl_skips_query_within_ttl(engine) -> None:
    """첫 호출은 반드시 묻고, TTL 안의 둘째 호출은 DB 없이 같은 한 벌을 준다."""
    cat = DbCatalogReader(engine, poll_ttl_s=60)
    calls = _count_polls(cat)
    first = cat.active()
    assert calls[0] == 1
    assert cat.active() is first and calls[0] == 1


def test_poll_ttl_zero_polls_every_call(engine) -> None:
    """TTL 0 = 옛 동작 — 호출마다 묻는다."""
    cat = DbCatalogReader(engine, poll_ttl_s=0)
    calls = _count_polls(cat)
    cat.active()
    cat.active()
    assert calls[0] == 2


def test_poll_ttl_expires_with_injected_clock(engine) -> None:
    now = [0.0]
    cat = DbCatalogReader(engine, poll_ttl_s=1.0, clock=lambda: now[0])
    calls = _count_polls(cat)
    first = cat.active()
    now[0] = 0.999
    cat.active()
    assert calls[0] == 1
    now[0] = 1.0
    assert cat.active() is first and calls[0] == 2                 # 다시 물었지만 버전이 같아 한 벌은 그대로


def test_poll_failure_is_not_cached_and_recovery_is_immediate(engine, active) -> None:
    """활성 해제는 TTL 안에서는 안 보이고(캐시), 지나면 NoActiveCatalog. 실패는 캐시하지 않아 되살리면 곧바로 회복."""
    version_id, _, _ = active
    now = [0.0]
    cat = DbCatalogReader(engine, poll_ttl_s=1.0, clock=lambda: now[0])
    calls = _count_polls(cat)
    cat.active()
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = false where id = :v"), {"v": version_id})
    try:
        assert cat.active()[0] == version_id and calls[0] == 1        # TTL 안 — 아직 캐시
        now[0] = 1.0
        with pytest.raises(NoActiveCatalog, match="활성 버전이 없습니다"):
            cat.active()
        with pytest.raises(NoActiveCatalog):
            cat.active()
        assert calls[0] == 3                                          # 실패 뒤 같은 시각에도 다시 물었다 — 실패는 캐시하지 않는다
    finally:
        with engine.begin() as c:
            c.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = true where id = :v"), {"v": version_id})
    assert cat.active()[0] == version_id and calls[0] == 4            # 시계 그대로인데 회복
