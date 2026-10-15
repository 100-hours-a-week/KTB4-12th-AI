"""Backend 전달 패키지 → ai_catalog 적재.

  적재:      uv run python -m tools.catalog.load_catalog --package ~/Downloads/product-catalog-20260922-v1 \
                 --id-map <번호표>/product-id-map.jsonl \
                 --category-id-map <번호표>/category-id-map.jsonl \
                 --metrics <번호표>/metrics.jsonl
              번호표 · 패키지는 팀 내부 자료라 저장소에 없다(10-15) — 프로파일링 담당에게 받는다
  확인만:    … --dry-run
  재고·조회수만 다시: … --metrics <metrics.jsonl>          (패키지 없이)

**서버(ECS) 적재 — 합본 파일 하나(10-15).** 패키지 · 번호표는 이미지에 없다. 로컬에서 Backend 번호로 합친 파일 하나를 만들어
비공개 S3 에 올리고, 적재 Task 는 그 파일만 읽는다(Task Role 로 S3 읽기).
  합본 만들기(DB 안 씀):  … --package <패키지> --id-map … --category-id-map … --metrics … --write-bundle catalog-bundle.json.gz
  합본 적재:              … --bundle s3://<버킷>/<키>  (또는 로컬 경로)  [--if-empty]
  --if-empty            활성 카탈로그가 이미 있으면 아무것도 하지 않고 성공(0) — 배포 워크플로가 매번 불러도 된다

패키지는 `product-catalog-20260922-v1` 모양을 기대한다 (data/categories.json · data/products.jsonl · data/summary.json).
적재는 트랜잭션 하나다 — 도중에 실패하면 아무것도 남지 않고 활성 버전도 그대로다.

**표의 키는 Backend 번호다(0008).** categories.id · products.id 는 Backend 의 categories.id · products.id 와
같은 번호다. 패키지에는 수집처 ID(KAKAO_GIFT:… · CAT-01-02)만 있으므로, 적재할 때 id-map 두 개로 Backend 번호를 붙인다.
Backend 번호가 없는 상품 · 카테고리가 하나라도 있으면 적재하지 않는다(키를 만들 수 없다). 수집처 상품 ID 는
products.source_product_id(키 아님)에 원본 추적용으로만 남는다.

종료 코드: 0 성공 · 1 패키지 형식 오류 · 2 DB 오류 · 3 확인만 하고 끝(dry-run)
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from profiling.settings import get_settings

SCHEMA = "ai_catalog"
BUNDLE_FORMAT = "ai-catalog-bundle/v1"
AVAILABILITY = ("available", "unavailable", "unknown")   # DB CHECK 와 같은 값


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


def _rows_from_package(pkg: dict[str, Any], product_ids: dict[str, int], category_ids: dict[str, int]
                       ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """패키지 + 번호표 → (버전 행, 카테고리 행, 상품 행). 키는 Backend 번호. 빠진 번호는 check_ids 가 먼저 막는다."""
    problems = check_ids(pkg, product_ids, category_ids)
    if problems:
        raise PackageError("; ".join(problems))
    summary, cats, prods = pkg["summary"], pkg["categories"], pkg["products"]
    package_id = summary.get("packageId", "unknown")
    taxonomy_version = pkg["taxonomy_version"] or summary.get("taxonomyVersion", "")
    meta = {"package_id": package_id, "taxonomy_version": taxonomy_version,
            "source_sha256": summary.get("sourceFileSha256")}
    cat_rows = [{"id": category_ids[c["sourceCategoryId"]],
                 "parent": category_ids[c["parentSourceCategoryId"]] if c["parentSourceCategoryId"] else None, "name": c["name"],
                 "level": c["level"], "count": c["productCount"], "taxonomy_version": taxonomy_version}
                for c in cats]
    prod_rows = [{"id": product_ids[p["sourceProductId"]], "sid": p["sourceProductId"],
                  "name": p["productName"], "brand": p["brandName"],
                  "category": category_ids[p["sourceCategoryId"]], "kind": p["productKind"], "type": p["productType"],
                  "description": p["description"], "attributes": p["attributes"],
                  "unit_price": p["unitPrice"], "list_price": p["listPrice"], "currency": p["currency"],
                  "stock_quantity": p["stockQuantity"],
                  "provider": p["source"]["provider"], "product_url": p["source"]["productUrl"],
                  "image_url": p["source"]["imageUrl"],
                  "asset_id": (p["images"][0]["assetId"] if p.get("images") else None),
                  "package_id": package_id}
                 for p in prods]
    return meta, cat_rows, prod_rows


def _write(engine: sa.Engine, meta: dict[str, Any], cat_rows: list[dict[str, Any]], prod_rows: list[dict[str, Any]], *,
           metrics: list[dict[str, Any]] | None = None, activate: bool = True) -> dict[str, Any]:
    """트랜잭션 하나로 버전 · 카테고리 · 상품(+ 재고 · 조회수)을 넣고 활성 버전을 바꾼다."""
    cat_rows = sorted(cat_rows, key=lambda c: (c["level"], c["id"]))          # 대분류 먼저 — FK 때문에
    prod_params = [{**r, "attributes": json.dumps(r["attributes"], ensure_ascii=False)} for r in prod_rows]
    with engine.begin() as conn:
        version_id = conn.execute(_VERSION, {
            "package_id": meta["package_id"], "taxonomy_version": meta["taxonomy_version"],
            "product_count": len(prod_rows), "category_count": len(cat_rows),
            "source_sha256": json.dumps(meta.get("source_sha256"), ensure_ascii=False),
        }).scalar_one()
        conn.execute(_CATEGORY, cat_rows)
        conn.execute(_PRODUCT, prod_params)
        if metrics:
            conn.execute(sa.text(f"""update {SCHEMA}.products set availability = coalesce(:av, availability),
                                         view_count = coalesce(:vc, view_count), updated_at = now() where id = :id"""), metrics)
        if activate:
            conn.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = false where is_active"))
            conn.execute(sa.text(f"update {SCHEMA}.catalog_versions set is_active = true where id = :id"), {"id": version_id})
    return {"version_id": version_id, "categories": len(cat_rows), "products": len(prod_rows), "package_id": meta["package_id"]}


def load(engine: sa.Engine, pkg: dict[str, Any], product_ids: dict[str, int], category_ids: dict[str, int], *,
         activate: bool = True) -> dict[str, Any]:
    """트랜잭션 하나로 카테고리·상품·버전을 넣는다. 활성 버전 교체까지 같은 트랜잭션.
    product_ids · category_ids 는 수집처 ID → Backend 번호(read_id_map). 빠진 것이 있으면 check_ids 가 먼저 막는다."""
    meta, cat_rows, prod_rows = _rows_from_package(pkg, product_ids, category_ids)
    return _write(engine, meta, cat_rows, prod_rows, activate=activate)


# ---------------------------------------------------------------------------
# 합본 파일 — 서버(ECS) 적재용. Backend 번호로 이미 합쳐 둔 카탈로그 한 벌(10-15)
# ---------------------------------------------------------------------------


def read_metrics_map(path: Path) -> dict[str, dict[str, Any]]:
    """metrics.jsonl → {수집처 상품 ID: {availability, viewCount}} (합본 만들 때만)."""
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                if item.get("availability") is not None and item["availability"] not in AVAILABILITY:
                    raise PackageError(f"재고 상태가 3값이 아니다: {item['availability']} ({item.get('sourceProductId')})")
                out[item["sourceProductId"]] = {"availability": item.get("availability"), "viewCount": item.get("viewCount")}
    return out


def build_bundle(pkg: dict[str, Any], product_ids: dict[str, int], category_ids: dict[str, int],
                 metrics: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """패키지 + 번호표(+ 재고 · 조회수) → 합본 dict. 순서는 번호 오름차순이라 같은 입력이면 같은 파일이다."""
    meta, cat_rows, prod_rows = _rows_from_package(pkg, product_ids, category_ids)
    metrics = metrics or {}
    return {
        "format": BUNDLE_FORMAT,
        "packageId": meta["package_id"], "taxonomyVersion": meta["taxonomy_version"], "sourceFileSha256": meta["source_sha256"],
        "categories": [{"id": c["id"], "parentId": c["parent"], "name": c["name"], "level": c["level"], "productCount": c["count"]}
                       for c in sorted(cat_rows, key=lambda c: c["id"])],
        "products": [{"id": r["id"], "categoryId": r["category"], "name": r["name"], "brand": r["brand"],
                      "productKind": r["kind"], "productType": r["type"], "description": r["description"],
                      "attributes": r["attributes"], "unitPrice": r["unit_price"], "listPrice": r["list_price"],
                      "currency": r["currency"], "stockQuantity": r["stock_quantity"],
                      "availability": metrics.get(r["sid"], {}).get("availability"),
                      "viewCount": metrics.get(r["sid"], {}).get("viewCount"),
                      "sourceProductId": r["sid"], "sourceProvider": r["provider"], "sourceProductUrl": r["product_url"],
                      "sourceImageUrl": r["image_url"], "imageAssetId": r["asset_id"]}
                     for r in sorted(prod_rows, key=lambda r: r["id"])],
    }


def write_bundle(bundle: dict[str, Any], path: Path) -> None:
    """합본을 파일로. 이름이 .gz 로 끝나면 gzip(시각을 0 으로 고정 — 같은 입력이면 같은 바이트)."""
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if path.name.endswith(".gz"):
        raw = gzip.compress(raw, mtime=0)
    path.write_bytes(raw)


def _s3_get(uri: str) -> bytes:
    """s3://버킷/키 → 바이트. 자격은 boto3 기본 경로(ECS 는 Task Role). boto3 는 이때만 불러온다."""
    # 로컬 · 시험에서는 필요 없어 여기서 불러온다
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    if not bucket or not key:
        raise PackageError(f"S3 주소 형식이 아니다: {uri} (s3://버킷/키)")
    try:
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except (BotoCoreError, ClientError) as e:              # 권한 · 없는 키 · 네트워크 — 원인 종류만(자격 정보는 남기지 않음)
        raise PackageError(f"S3 에서 합본을 읽지 못했다({type(e).__name__}): {uri}") from e


def read_bundle(src: str) -> dict[str, Any]:
    """합본 읽기 — 로컬 경로 또는 s3://…, gzip 이면 풀어서."""
    try:
        raw = _s3_get(src) if src.startswith("s3://") else Path(src).read_bytes()
    except FileNotFoundError as e:
        raise PackageError(f"합본 파일이 없다: {src}") from e
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PackageError(f"합본 JSON 을 읽을 수 없다: {e}") from e
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise PackageError(f"합본 형식이 아니다(format={bundle.get('format') if isinstance(bundle, dict) else None}, 기대 {BUNDLE_FORMAT})")
    return bundle


def check_bundle(bundle: dict[str, Any]) -> list[str]:
    """합본 적재 전 확인 — 번호 양수 · 중복 없음 · 계층 · 상품의 소분류 · 재고 3값."""
    problems: list[str] = []
    cats, prods = bundle.get("categories") or [], bundle.get("products") or []
    if not cats or not prods:
        return ["카테고리 또는 상품이 비었다"]
    cids = [c["id"] for c in cats]
    pids = [p["id"] for p in prods]
    for kind, ids in (("카테고리", cids), ("상품", pids)):
        if len(set(ids)) != len(ids):
            problems.append(f"{kind} 번호가 겹친다 {len(ids) - len(set(ids))}건")
        if any(not isinstance(i, int) or i <= 0 for i in ids):
            problems.append(f"{kind} 번호가 양의 정수가 아닌 것이 있다")
    top = {c["id"] for c in cats if c["level"] == 1}
    leaf = {c["id"] for c in cats if c["level"] == 2}
    bad = [c["id"] for c in cats if (c["level"] == 1) != (c.get("parentId") is None) or (c["level"] == 2 and c["parentId"] not in top)]
    if bad:
        problems.append(f"계층이 맞지 않는 카테고리 {len(bad)}건: {bad[:5]}")
    outside = [p["id"] for p in prods if p["categoryId"] not in leaf]
    if outside:
        problems.append(f"소분류에 없는 카테고리를 가리키는 상품 {len(outside)}건: {outside[:5]}")
    wrong = [p["id"] for p in prods if p.get("availability") is not None and p["availability"] not in AVAILABILITY]
    if wrong:
        problems.append(f"재고 상태가 3값이 아닌 상품 {len(wrong)}건: {wrong[:5]}")
    return problems


def load_bundle(engine: sa.Engine, bundle: dict[str, Any], *, activate: bool = True) -> dict[str, Any]:
    """합본 → 세 표. 재고 · 조회수도 같은 트랜잭션에서 넣는다."""
    problems = check_bundle(bundle)
    if problems:
        raise PackageError("; ".join(problems))
    meta = {"package_id": bundle.get("packageId") or "unknown", "taxonomy_version": bundle.get("taxonomyVersion") or "",
            "source_sha256": bundle.get("sourceFileSha256")}
    cat_rows = [{"id": c["id"], "parent": c.get("parentId"), "name": c["name"], "level": c["level"],
                 "count": c.get("productCount", 0), "taxonomy_version": meta["taxonomy_version"]} for c in bundle["categories"]]
    prod_rows = [{"id": p["id"], "sid": p.get("sourceProductId"), "name": p["name"], "brand": p["brand"],
                  "category": p["categoryId"], "kind": p["productKind"], "type": p["productType"],
                  "description": p["description"], "attributes": p.get("attributes") or {},
                  "unit_price": p["unitPrice"], "list_price": p.get("listPrice"), "currency": p.get("currency") or "KRW",
                  "stock_quantity": p.get("stockQuantity"), "provider": p.get("sourceProvider") or "",
                  "product_url": p.get("sourceProductUrl") or "", "image_url": p.get("sourceImageUrl") or "",
                  "asset_id": p.get("imageAssetId"), "package_id": meta["package_id"]} for p in bundle["products"]]
    metrics = [{"id": p["id"], "av": p.get("availability"), "vc": p.get("viewCount")} for p in bundle["products"]
               if p.get("availability") is not None or p.get("viewCount") is not None]
    return _write(engine, meta, cat_rows, prod_rows, metrics=metrics, activate=activate)


def has_active_catalog(engine: sa.Engine) -> bool:
    """활성 카탈로그(상품이 있는)가 이미 있나 — --if-empty 판단."""
    with engine.connect() as conn:
        return bool(conn.execute(sa.text(
            f"""select exists(select 1 from {SCHEMA}.catalog_versions v join {SCHEMA}.products p on p.package_id = v.package_id
                where v.is_active)""")).scalar())


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
    ap.add_argument("--write-bundle", type=Path, help="DB 에 쓰지 않고 합본 파일(.json 또는 .json.gz)만 만든다 — 서버 적재용(10-15)")
    ap.add_argument("--bundle", help="합본 파일을 적재한다 — 로컬 경로 또는 s3://버킷/키 (서버 적재)")
    ap.add_argument("--if-empty", action="store_true", help="활성 카탈로그가 이미 있으면 아무것도 하지 않고 성공 — 배포 워크플로용")
    args = ap.parse_args(argv)

    if args.bundle:
        if args.package or args.id_map or args.category_id_map or args.metrics or args.write_bundle:
            ap.error("--bundle 은 단독으로 쓴다(합본에 번호 · 재고 · 조회수가 다 들어 있다)")
        return _main_bundle(args)
    if args.write_bundle and not args.package:
        ap.error("--write-bundle 에는 --package · --id-map · --category-id-map 이 필요하다")
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
        if args.write_bundle:
            try:
                bundle = build_bundle(pkg, product_ids, category_ids, read_metrics_map(args.metrics) if args.metrics else None)
            except PackageError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
            write_bundle(bundle, args.write_bundle)
            print(f"  ✓ 합본 {args.write_bundle} — 카테고리 {len(bundle['categories'])} · 상품 {len(bundle['products'])} · "
                  f"{args.write_bundle.stat().st_size:,} bytes (팀 내부 자료 — 비공개 S3 에만 올린다)")
            return 0
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


def _main_bundle(args: argparse.Namespace) -> int:
    """--bundle 경로 — 합본 읽기 → (--if-empty 면 이미 있을 때 건너뜀) → 적재."""
    try:
        bundle = read_bundle(args.bundle)
    except PackageError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    problems = check_bundle(bundle)
    print(f"합본 {bundle.get('packageId')} · 분류 {bundle.get('taxonomyVersion')} · 카테고리 {len(bundle['categories'])} · 상품 {len(bundle['products'])}")
    for p in problems:
        print(f"  ✗ {p}", file=sys.stderr)
    if problems:
        return 1
    if args.dry_run:
        print("dry-run — 쓰지 않았다")
        return 3
    engine = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        if args.if_empty and has_active_catalog(engine):
            print("  · 활성 카탈로그가 이미 있다 — --if-empty 라 적재하지 않는다")
            return 0
        got = load_bundle(engine, bundle, activate=not args.no_activate)
        print(f"  ✓ 적재 — 카테고리 {got['categories']} · 상품 {got['products']} · 버전 {got['version_id']}"
              f"{'' if args.no_activate else ' (활성)'}")
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
