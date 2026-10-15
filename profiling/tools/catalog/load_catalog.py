"""Backend 전달 패키지 → ai_catalog 적재 (마이그레이션 0003 · 0008).

  적재:      uv run python -m tools.catalog.load_catalog --package ~/Downloads/product-catalog-20260922-v1 \
                 --id-map tools/catalog/returned/2026-09-25/product-id-map.jsonl \
                 --category-id-map tools/catalog/returned/2026-09-25/category-id-map.jsonl \
                 --metrics tools/catalog/returned/2026-09-25/metrics.jsonl
  확인만:    … --dry-run
  재고·조회수만 다시: … --metrics <metrics.jsonl>          (패키지 없이)
              회신 xlsx 를 id-map · metrics jsonl 로 바꾸는 것은 tools/catalog/import_be_ids.py

패키지는 `product-catalog-20260922-v1` 모양을 기대한다 (data/categories.json · data/products.jsonl · data/summary.json).
적재는 트랜잭션 하나다 — 도중에 실패하면 아무것도 남지 않고 활성 버전도 그대로다.

**표의 키는 Backend 번호다(0008).** categories.id · products.id 는 Backend 상품 시드(categories.id 1~67 · products.id 1~4231)와
같은 번호다. 패키지에는 수집처 ID(KAKAO_GIFT:… · CAT-01-02)만 있으므로, 적재할 때 id-map 두 개로 Backend 번호를 붙인다.
Backend 번호가 없는 상품 · 카테고리가 하나라도 있으면 적재하지 않는다(키를 만들 수 없다). 수집처 상품 ID 는
products.source_product_id(키 아님)에 원본 추적용으로만 남는다.

종료 코드: 0 성공 · 1 패키지 형식 오류 · 2 DB 오류 · 3 확인만 하고 끝(dry-run)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from profiling.settings import get_settings

SCHEMA = "ai_catalog"
AVAILABILITY = ("available", "unavailable", "unknown")   # 마이그레이션 0003 의 CHECK 와 같은 값


# ---------------------------------------------------------------------------
# 읽기·검사
# ---------------------------------------------------------------------------


class PackageError(Exception):
    """패키지 형식이 기대와 다르다."""


def read_package(root: Path) -> dict[str, Any]:
    """패키지 폴더 → {summary, categories, products}. 형식이 다르면 PackageError."""
    try:
        summary = json.loads((root / "data/summary.json").read_text(encoding="utf-8"))
        envelope = json.loads((root / "data/categories.json").read_text(encoding="utf-8"))
        with (root / "data/products.jsonl").open(encoding="utf-8") as f:
            products = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError as e:
        raise PackageError(f"패키지 파일이 없다: {e.filename}") from e
    except json.JSONDecodeError as e:
        raise PackageError(f"JSON을 읽을 수 없다: {e}") from e

    categories = envelope.get("categories")
    if not isinstance(categories, list) or not categories:
        raise PackageError("data/categories.json 에 categories 배열이 없다")
    return {"summary": summary, "taxonomy_version": envelope.get("taxonomyVersion", ""),
            "categories": categories, "products": products}


def read_id_map(path: Path, *, src: str, dst: str) -> dict[str, int]:
    """회신 jsonl → {수집처 ID: Backend 번호}. null 인 줄은 건너뛴다(그 상품은 check 가 '번호 없음'으로 잡는다)."""
    out: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                if item.get(dst) is not None:
                    out[item[src]] = int(item[dst])
    return out


def check_ids(pkg: dict[str, Any], product_ids: dict[str, int], category_ids: dict[str, int]) -> list[str]:
    """Backend 번호가 패키지 전부에 붙는지 · 겹치지 않는지. 문제를 문장 목록으로."""
    problems: list[str] = []
    for kind, items, key, ids in (("카테고리", pkg["categories"], "sourceCategoryId", category_ids),
                                  ("상품", pkg["products"], "sourceProductId", product_ids)):
        missing = [i[key] for i in items if i[key] not in ids]
        if missing:
            problems.append(f"Backend 번호가 없는 {kind} {len(missing)}건: {', '.join(missing[:5])}")
        used = [ids[i[key]] for i in items if i[key] in ids]
        if len(set(used)) != len(used):
            problems.append(f"{kind} Backend 번호가 겹친다 {len(used) - len(set(used))}건")
        if any(b <= 0 for b in used):
            problems.append(f"{kind} Backend 번호가 0 이하인 것이 있다")
    return problems


def check(pkg: dict[str, Any]) -> list[str]:
    """적재 전 확인. 문제를 문장 목록으로 돌려준다(빈 목록이면 통과)."""
    problems: list[str] = []
    cats, prods = pkg["categories"], pkg["products"]
    ids = {c["sourceCategoryId"] for c in cats}
    leaf = {c["sourceCategoryId"] for c in cats if c["level"] == 2}

    if len(ids) != len(cats):
        problems.append(f"카테고리 ID 중복 {len(cats) - len(ids)}건")
    orphan = [c["sourceCategoryId"] for c in cats if c["level"] == 2 and c["parentSourceCategoryId"] not in ids]
    if orphan:
        problems.append(f"부모가 없는 소분류 {len(orphan)}건: {', '.join(orphan[:5])}")

    pids = {p["sourceProductId"] for p in prods}
    if len(pids) != len(prods):
        problems.append(f"sourceProductId 중복 {len(prods) - len(pids)}건")
    outside = [p["sourceProductId"] for p in prods if p["sourceCategoryId"] not in leaf]
    if outside:
        problems.append(f"소분류에 없는 카테고리를 가리키는 상품 {len(outside)}건: {', '.join(outside[:5])}")

    declared = pkg["summary"].get("productCount")
    if declared is not None and declared != len(prods):
        problems.append(f"summary.productCount={declared} 인데 실제 {len(prods)}건")
    return problems


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------

_VERSION = sa.text(f"""
    insert into {SCHEMA}.catalog_versions (package_id, taxonomy_version, product_count, category_count, source_sha256)
    values (:package_id, :taxonomy_version, :product_count, :category_count, cast(:source_sha256 as jsonb))
    returning id""")

_CATEGORY = sa.text(f"""
    insert into {SCHEMA}.categories
        (id, parent_id, name, level, product_count, taxonomy_version)
    values (:id, :parent, :name, :level, :count, :taxonomy_version)
    on conflict (id) do update set
        parent_id = excluded.parent_id,
        name = excluded.name, level = excluded.level,
        product_count = excluded.product_count, taxonomy_version = excluded.taxonomy_version,
        updated_at = now()""")

_PRODUCT = sa.text(f"""
    insert into {SCHEMA}.products
        (id, source_product_id, name, brand, category_id, product_kind, product_type, description,
         attributes, unit_price, list_price, currency, stock_quantity,
         source_provider, source_product_url, source_image_url, image_asset_id, package_id)
    values (:id, :sid, :name, :brand, :category, :kind, :type, :description,
            cast(:attributes as jsonb), :unit_price, :list_price, :currency, :stock_quantity,
            :provider, :product_url, :image_url, :asset_id, :package_id)
    on conflict (id) do update set
        source_product_id = excluded.source_product_id,
        name = excluded.name, brand = excluded.brand, category_id = excluded.category_id,
        product_kind = excluded.product_kind, product_type = excluded.product_type, description = excluded.description,
        attributes = excluded.attributes, unit_price = excluded.unit_price, list_price = excluded.list_price,
        currency = excluded.currency, stock_quantity = excluded.stock_quantity,
        source_provider = excluded.source_provider, source_product_url = excluded.source_product_url,
        source_image_url = excluded.source_image_url, image_asset_id = excluded.image_asset_id,
        package_id = excluded.package_id, updated_at = now()""")
# availability · view_count 은 여기서 건드리지 않는다 — --metrics(Backend 회신)가 채운다.
# (재적재가 Backend 의 재고 상태 · 조회수를 지우면 안 된다. 패키지에는 그 둘이 아예 없다.)


def load(engine: sa.Engine, pkg: dict[str, Any], product_ids: dict[str, int], category_ids: dict[str, int], *,
         activate: bool = True) -> dict[str, Any]:
    """트랜잭션 하나로 카테고리·상품·버전을 넣는다. 활성 버전 교체까지 같은 트랜잭션.
    product_ids · category_ids 는 수집처 ID → Backend 번호(read_id_map). 빠진 것이 있으면 check_ids 가 먼저 막는다."""
    problems = check_ids(pkg, product_ids, category_ids)
    if problems:
        raise PackageError("; ".join(problems))
    summary, cats, prods = pkg["summary"], pkg["categories"], pkg["products"]
    package_id = summary.get("packageId", "unknown")
    taxonomy_version = pkg["taxonomy_version"] or summary.get("taxonomyVersion", "")

    cat_rows = [{"id": category_ids[c["sourceCategoryId"]],
                 "parent": category_ids[c["parentSourceCategoryId"]] if c["parentSourceCategoryId"] else None, "name": c["name"],
                 "level": c["level"], "count": c["productCount"], "taxonomy_version": taxonomy_version}
                for c in sorted(cats, key=lambda c: c["level"])]          # 대분류 먼저 — FK 때문에
    prod_rows = [{"id": product_ids[p["sourceProductId"]], "sid": p["sourceProductId"],
                  "name": p["productName"], "brand": p["brandName"],
                  "category": category_ids[p["sourceCategoryId"]], "kind": p["productKind"], "type": p["productType"],
                  "description": p["description"], "attributes": json.dumps(p["attributes"], ensure_ascii=False),
                  "unit_price": p["unitPrice"], "list_price": p["listPrice"], "currency": p["currency"],
                  "stock_quantity": p["stockQuantity"],
                  "provider": p["source"]["provider"], "product_url": p["source"]["productUrl"],
                  "image_url": p["source"]["imageUrl"],
                  "asset_id": (p["images"][0]["assetId"] if p.get("images") else None),
                  "package_id": package_id}
                 for p in prods]

    with engine.begin() as conn:
        version_id = conn.execute(_VERSION, {
            "package_id": package_id, "taxonomy_version": taxonomy_version,
            "product_count": len(prods), "category_count": len(cats),
            "source_sha256": json.dumps(summary.get("sourceFileSha256"), ensure_ascii=False),
        }).scalar_one()
        conn.execute(_CATEGORY, cat_rows)
        conn.execute(_PRODUCT, prod_rows)
        if activate:
            conn.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = false where is_active"))
            conn.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = true where id = :id"), {"id": version_id})
    return {"version_id": version_id, "categories": len(cat_rows), "products": len(prod_rows), "package_id": package_id}


# ---------------------------------------------------------------------------
# 재고 · 조회수 회신 반영
# ---------------------------------------------------------------------------


def apply_metrics(engine: sa.Engine, path: Path) -> dict[str, int]:
    """재고·조회수 회신(JSONL) → availability · view_count 채우기. 그 두 열만 건드린다.

    한 줄: {"productId": 1, "availability": "available", "viewCount": 47753}   — productId 는 Backend 번호(products.id)
          옛 회신 형식 {"sourceProductId": "KAKAO_GIFT:1", …} 도 받는다(products.source_product_id 로 찾는다).
    availability 는 3값만 받는다 — 이상한 값은 DB CHECK 가 막지만 여기서 먼저 거른다.
    """
    rows, skipped = [], 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            av, vc = item.get("availability"), item.get("viewCount")
            if av is None and vc is None:
                skipped += 1
                continue
            if av is not None and av not in AVAILABILITY:
                raise PackageError(f"재고 상태가 3값이 아니다: {av} ({item.get('sourceProductId')})")
            by_id = item.get("productId")
            rows.append({"id": None if by_id is None else int(by_id), "sid": item.get("sourceProductId"),
                         "av": av, "vc": None if vc is None else int(vc)})
    if not rows:
        return {"updated": 0, "skipped": skipped, "missing": 0}

    sets = """availability = coalesce(:av, availability),
              view_count   = coalesce(:vc, view_count),
              updated_at   = now()"""
    by_id = sa.text(f"update {SCHEMA}.products set {sets} where id = :id")
    by_source = sa.text(f"update {SCHEMA}.products set {sets} where source_product_id = :sid")
    updated = 0
    with engine.begin() as conn:
        for row in rows:
            updated += conn.execute(by_id if row["id"] is not None else by_source, row).rowcount
    return {"updated": updated, "skipped": skipped, "missing": len(rows) - updated}


# ---------------------------------------------------------------------------
# 보고
# ---------------------------------------------------------------------------


def report(engine: sa.Engine) -> None:
    """적재 결과 요약 — 건수·계층·Backend ID 채움 정도."""
    with engine.begin() as conn:
        def q(sql: str):
            return conn.execute(sa.text(sql)).scalar_one()

        print(f"  카테고리        대분류 {q(f'select count(*) from {SCHEMA}.categories where level = 1')}"
              f" · 소분류 {q(f'select count(*) from {SCHEMA}.categories where level = 2')}")
        print(f"  상품            {q(f'select count(*) from {SCHEMA}.products')}건")
        print(f"  활성 버전       {q(f'select coalesce(max(id::text), chr(45)) from {SCHEMA}.catalog_versions where is_active')}")
        print(f"  Backend 번호    상품 {q(f'select coalesce(min(id), 0) from {SCHEMA}.products')}~{q(f'select coalesce(max(id), 0) from {SCHEMA}.products')}"
              f" · 카테고리 {q(f'select coalesce(min(id), 0) from {SCHEMA}.categories')}~{q(f'select coalesce(max(id), 0) from {SCHEMA}.categories')} (표의 키)")
        av = q(f"select count(*) from {SCHEMA}.products where availability <> 'unknown'")
        vc = q(f"select count(view_count) from {SCHEMA}.products")
        total = q(f"select count(*) from {SCHEMA}.products")
        print(f"  availability    {av}/{total} 확인됨" + ("" if av == total else " (나머지는 unknown — 재고 정보가 없는 상품)"))
        print(f"  view_count      {vc}/{total} 있음 — v1 풀 정렬 기준" + ("" if vc == total else ". Backend 회신으로 채운다"))
        mismatch = conn.execute(sa.text(f"""
            select c.id, c.product_count, count(p.id)
            from {SCHEMA}.categories c left join {SCHEMA}.products p on p.category_id = c.id
            where c.level = 2 group by c.id, c.product_count
            having c.product_count <> count(p.id)""")).all()
        print(f"  선언 수 대조    {'전부 일치' if not mismatch else f'{len(mismatch)}개 소분류 불일치: {mismatch[:3]}'}")


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backend 전달 패키지를 ai_catalog 에 적재한다")
    ap.add_argument("--package", type=Path, help="패키지 폴더 (product-catalog-YYYYMMDD-vN)")
    ap.add_argument("--id-map", type=Path, help="회신 product-id-map.jsonl — 수집처 상품 ID → Backend 상품 번호(--package 와 함께 필수)")
    ap.add_argument("--category-id-map", type=Path, help="회신 category-id-map.jsonl — 수집처 카테고리 ID → Backend 카테고리 번호(--package 와 함께 필수)")
    ap.add_argument("--metrics", type=Path, help="회신 metrics.jsonl — availability · view_count 채우기")
    ap.add_argument("--dry-run", action="store_true", help="확인만 하고 쓰지 않는다")
    ap.add_argument("--no-activate", action="store_true", help="적재는 하되 활성 버전으로 바꾸지 않는다")
    args = ap.parse_args(argv)

    if not (args.package or args.metrics):
        ap.error("--package(+ --id-map · --category-id-map) 또는 --metrics 중 하나는 필요하다")
    if args.package and not (args.id_map and args.category_id_map):
        ap.error("--package 에는 --id-map 과 --category-id-map 이 함께 필요하다 — 표의 키가 Backend 번호다(0008)")
    if (args.id_map or args.category_id_map) and not args.package:
        ap.error("--id-map · --category-id-map 은 --package 와 함께만 쓴다(번호는 적재할 때 키로 붙는다)")

    pkg = None
    product_ids: dict[str, int] = {}
    category_ids: dict[str, int] = {}
    if args.package:
        try:
            pkg = read_package(args.package)
        except PackageError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        product_ids = read_id_map(args.id_map, src="sourceProductId", dst="backendProductId")
        category_ids = read_id_map(args.category_id_map, src="sourceCategoryId", dst="backendCategoryId")
        problems = check(pkg) + check_ids(pkg, product_ids, category_ids)
        print(f"패키지 {pkg['summary'].get('packageId')} · 분류 {pkg['taxonomy_version']}")
        print(f"  카테고리 {len(pkg['categories'])} · 상품 {len(pkg['products'])}")
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        if problems:
            return 1
        print("  ✓ 형식 확인 통과")
        if args.dry_run:
            print("dry-run — 쓰지 않았다")
            return 3

    engine = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with engine.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        print(f"✗ DB 연결 실패: {e.orig if hasattr(e, 'orig') else e}\n  → docker compose up -d && uv run alembic upgrade head",
              file=sys.stderr)
        return 2

    try:
        if pkg is not None:
            got = load(engine, pkg, product_ids, category_ids, activate=not args.no_activate)
            print(f"  ✓ 적재 — 카테고리 {got['categories']} · 상품 {got['products']} · 버전 {got['version_id']}"
                  f"{'' if args.no_activate else ' (활성)'}")
        if args.metrics:
            r = apply_metrics(engine, args.metrics)
            print(f"  ✓ 재고·조회수 회신 — 채움 {r['updated']} · 값 없어 건너뜀 {r['skipped']} · 우리 표에 없는 ID {r['missing']}")
        print("\n현재 상태")
        report(engine)
    except PackageError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except sa.exc.SQLAlchemyError as e:
        print(f"✗ DB 오류: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
