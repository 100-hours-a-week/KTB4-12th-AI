"""Backend 회신(xlsx 2종) → `--id-map`·`--metrics` 로 넣을 jsonl 3개.

  uv run python -m tools.catalog.import_be_ids \
      --categories ~/Downloads/카테고리_DB_ID_매핑_2026-09-23.xlsx \
      --products   ~/Downloads/v1_상품목록_4231개_2026-09-23.xlsx \
      --out        tools/catalog/returned/2026-09-25/

**DB에 쓰지 않는다.** 중간 파일을 만들고 끝낸다 — 반영은 `load_catalog`이 하고, 중간 파일이 남아야
"무엇을 왜 그렇게 이었나"를 나중에 확인할 수 있다.

BE 회신에는 **수집처 ID(KAKAO_GIFT:… · CAT-01-02)가 없다.** BE가 발급한 번호와 이름·가격뿐이라 이름으로 잇는다.
  카테고리  소분류·대분류 **이름**으로 (2026-09-23 회신 기준 57/57 · 10/10 일치)
  상품      (이름, 브랜드, 가격, 소분류 이름) 으로. 그래도 남는 중복은 양쪽을 정렬해 1:1로 붙인다
            (BE 표에 이름·브랜드·가격·분류가 완전히 같은 상품이 몇 건 있다 — 구분할 근거가 없다)

한 BE 번호가 두 상품에 붙으면 **즉시 실패**한다. 그대로 넣으면 uq_products_backend_id 에 걸리고,
설령 통과해도 다른 상품을 추천하게 된다.

종료 코드: 0 성공 · 1 파일·매칭 오류 · 2 DB 오류 · 3 확인만 하고 끝(dry-run)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from profiling.settings import get_settings
from tools.catalog._xlsx import XlsxError, rows

SCHEMA = "ai_catalog"

## 두 xlsx 의 열 순서 (2026-09-23 회신). 헤더 문구로 한 번 확인하고 쓴다.
CATEGORY_HEADER = ("대분류 ID", "대분류 이름", "소분류 ID", "소분류 이름")
PRODUCT_HEADER = ("상품 ID", "상품명", "브랜드", "대분류 ID", "대분류 이름", "소분류 ID", "소분류 이름", "가격(원)", "재고", "조회 수")


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------


def _table(path: Path, header: tuple[str, ...]) -> list[list[str | None]]:
    """머리글 줄을 찾아 그 아래 데이터 행만. 파일 맨 위 제목·설명 줄은 건너뛴다."""
    all_rows = rows(path)
    for i, r in enumerate(all_rows):
        if r[:len(header)] == list(header):
            data = [x for x in all_rows[i + 1:] if x and x[0] not in (None, "")]
            if not data:
                raise XlsxError(f"{path.name}: 머리글은 찾았으나 데이터 행이 없다")
            return data
    raise XlsxError(f"{path.name}: 기대한 머리글을 찾을 수 없다 — {' · '.join(header)}")


def read_categories(path: Path) -> list[dict[str, Any]]:
    return [{"top_id": int(r[0]), "top_name": r[1], "leaf_id": int(r[2]), "leaf_name": r[3]}
            for r in _table(path, CATEGORY_HEADER)]


def read_products(path: Path) -> list[dict[str, Any]]:
    return [{"backend_id": int(r[0]), "name": r[1], "brand": r[2], "leaf_name": r[6],
             "price": int(r[7]), "stock": int(r[8]), "views": int(r[9])}
            for r in _table(path, PRODUCT_HEADER)]


def read_ours(engine: sa.Engine) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """지금 ai_catalog 에 있는 상품·카테고리 (활성 버전의 패키지만)."""
    with engine.connect() as c:
        package = c.execute(sa.text(f"select package_id from {SCHEMA}.catalog_versions where is_active")).scalar()
        if package is None:
            raise RuntimeError(f"{SCHEMA}.catalog_versions 에 활성 버전이 없다 — 먼저 load_catalog 로 적재하세요")
        products = [dict(r) for r in c.execute(sa.text(
            f"""select p.source_product_id, p.name, p.brand, p.unit_price, c.name as leaf_name
                from {SCHEMA}.products p join {SCHEMA}.categories c on c.source_category_id = p.source_category_id
                where p.package_id = :pkg"""), {"pkg": package}).mappings()]
        categories = [dict(r) for r in c.execute(sa.text(
            f"select source_category_id, name, level from {SCHEMA}.categories")).mappings()]
    return products, categories


# ---------------------------------------------------------------------------
# 매칭 (순수 함수 — DB·파일 없이 시험한다)
# ---------------------------------------------------------------------------


@dataclass
class MatchReport:
    pairs: dict[str, int] = field(default_factory=dict)        # source_product_id → backend_id
    exact: int = 0                                             # 키 하나로 바로 확정
    tied: int = 0                                              # 중복이라 정렬해 붙인 것
    unmatched_ours: list[str] = field(default_factory=list)
    unmatched_theirs: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _key(name: str, brand: str, price: int, leaf: str) -> tuple:
    return (name, brand, int(price), leaf)


def match_products(ours: list[dict[str, Any]], theirs: list[dict[str, Any]]) -> MatchReport:
    """우리 상품 ↔ BE 상품을 1:1로 잇는다.

    같은 키에 여러 건이면 양쪽을 정렬해 순서대로 붙인다 — 구분할 정보가 없는 진짜 중복이므로 어느 배정이든
    같지만, 같은 입력이면 **항상 같은 결과**가 나와야 한다(다시 돌렸을 때 번호가 뒤바뀌면 추천이 바뀐다).
    """
    rep = MatchReport()
    ours_by_key: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for p in ours:
        ours_by_key[_key(p["name"], p["brand"], p["unit_price"], p["leaf_name"])].append(p)
    theirs_by_key: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for p in theirs:
        theirs_by_key[_key(p["name"], p["brand"], p["price"], p["leaf_name"])].append(p)

    for key, mine in sorted(ours_by_key.items()):
        yours = theirs_by_key.get(key, [])
        if not yours:
            rep.unmatched_ours.extend(sorted(p["source_product_id"] for p in mine))
            continue
        mine_sorted = sorted(mine, key=lambda p: p["source_product_id"])
        yours_sorted = sorted(yours, key=lambda p: p["backend_id"])
        single = len(mine_sorted) == 1 and len(yours_sorted) == 1
        for m, y in zip(mine_sorted, yours_sorted, strict=False):
            rep.pairs[m["source_product_id"]] = y["backend_id"]
            if single:
                rep.exact += 1
            else:
                rep.tied += 1
        if len(mine_sorted) > len(yours_sorted):                     # 우리 쪽이 더 많다 — 남는 것은 못 붙음
            rep.unmatched_ours.extend(p["source_product_id"] for p in mine_sorted[len(yours_sorted):])
        elif len(yours_sorted) > len(mine_sorted):
            rep.unmatched_theirs.extend(p["backend_id"] for p in yours_sorted[len(mine_sorted):])

    matched_theirs = set(rep.pairs.values())
    rep.unmatched_theirs.extend(sorted(p["backend_id"] for p in theirs if p["backend_id"] not in matched_theirs
                                       and p["backend_id"] not in rep.unmatched_theirs))
    if len(matched_theirs) != len(rep.pairs):                        # 한 번호가 두 상품에 붙었다
        rep.problems.append(f"Backend 상품 번호가 중복 배정됨 — 붙인 {len(rep.pairs)}건 중 고유 번호는 {len(matched_theirs)}개")
    return rep


def match_categories(ours: list[dict[str, Any]], theirs: list[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    """카테고리는 이름으로 잇는다. (source_category_id → backend id, 문제 목록)"""
    leaf = {c["leaf_name"]: c["leaf_id"] for c in theirs}
    top = {c["top_name"]: c["top_id"] for c in theirs}
    pairs: dict[str, int] = {}
    problems: list[str] = []
    for c in ours:
        table = leaf if c["level"] == 2 else top
        if c["name"] not in table:
            problems.append(f"{'소분류' if c['level'] == 2 else '대분류'} 이름이 회신에 없다: {c['name']} ({c['source_category_id']})")
            continue
        pairs[c["source_category_id"]] = table[c["name"]]
    if len(set(pairs.values())) != len(pairs):
        problems.append("Backend 카테고리 번호가 중복 배정됨 — 이름이 같은 분류가 둘 이상인지 확인")
    return pairs, problems


def availability_of(stock: int) -> str:
    """재고 → 3값. 회신에 재고 열이 있으므로 미확인(unknown)이 아니다."""
    return "available" if stock > 0 else "unavailable"


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backend 회신 xlsx → id-map · metrics jsonl")
    ap.add_argument("--categories", type=Path, required=True, help="카테고리 DB ID 매핑 xlsx")
    ap.add_argument("--products", type=Path, required=True, help="v1 상품목록 xlsx")
    ap.add_argument("--out", type=Path, required=True, help="jsonl 3개를 쓸 폴더")
    ap.add_argument("--dry-run", action="store_true", help="대조만 하고 쓰지 않는다")
    args = ap.parse_args(argv)

    try:
        be_categories = read_categories(args.categories)
        be_products = read_products(args.products)
    except (XlsxError, ValueError, TypeError) as e:
        print(f"✗ 회신 파일을 읽을 수 없다: {e}", file=sys.stderr)
        return 1
    print(f"회신 파일 — 카테고리 {len(be_categories)} · 상품 {len(be_products)}")

    engine = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        try:
            our_products, our_categories = read_ours(engine)
        except (sa.exc.OperationalError, sa.exc.ProgrammingError) as e:
            print(f"✗ DB 를 읽을 수 없다: {type(e).__name__}\n  → docker compose up -d && uv run alembic upgrade head", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
    finally:
        engine.dispose()
    print(f"우리 카탈로그 — 카테고리 {len(our_categories)} · 상품 {len(our_products)}")

    cat_pairs, cat_problems = match_categories(our_categories, be_categories)
    rep = match_products(our_products, be_products)

    print(f"  카테고리 매칭 {len(cat_pairs)}/{len(our_categories)}")
    print(f"  상품 매칭 {len(rep.pairs)}/{len(our_products)}  (키로 확정 {rep.exact} · 중복이라 정렬 배정 {rep.tied})")
    for line in cat_problems + rep.problems:
        print(f"  ✗ {line}", file=sys.stderr)
    for sid in rep.unmatched_ours[:10]:
        print(f"  ✗ 회신에 없는 우리 상품: {sid}", file=sys.stderr)
    if rep.unmatched_theirs:
        print(f"  ⚠ 우리 카탈로그에 없는 회신 상품 {len(rep.unmatched_theirs)}건 (예: {rep.unmatched_theirs[:5]})", file=sys.stderr)
    if cat_problems or rep.problems or rep.unmatched_ours:
        return 1

    by_backend_id = {p["backend_id"]: p for p in be_products}
    products_map = [{"sourceProductId": sid, "backendProductId": bid} for sid, bid in sorted(rep.pairs.items())]
    categories_map = [{"sourceCategoryId": cid, "backendCategoryId": bid} for cid, bid in sorted(cat_pairs.items())]
    metrics = [{"sourceProductId": sid, "availability": availability_of(by_backend_id[bid]["stock"]),
                "viewCount": by_backend_id[bid]["views"]} for sid, bid in sorted(rep.pairs.items())]

    stocks = {m["availability"] for m in metrics}
    print(f"  재고 → {' · '.join(sorted(stocks))} · 조회수 {min(m['viewCount'] for m in metrics)}~{max(m['viewCount'] for m in metrics)}")
    if args.dry_run:
        print("dry-run — 쓰지 않았다")
        return 3

    write_jsonl(args.out / "product-id-map.jsonl", products_map)
    write_jsonl(args.out / "category-id-map.jsonl", categories_map)
    write_jsonl(args.out / "metrics.jsonl", metrics)
    print(f"  ✓ {args.out}/ 에 product-id-map({len(products_map)}) · category-id-map({len(categories_map)}) · metrics({len(metrics)})")
    print("\n다음: uv run python -m tools.catalog.load_catalog --id-map … --category-id-map … --metrics …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
