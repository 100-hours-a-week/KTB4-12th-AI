"""예시 카탈로그 생성 — 동료 공유본(4,231건)에서 카테고리별로 조금씩 뽑아 7.9 export 형식 JSON을 만든다.
DB adapter·Catalog 빌드(#9~13) 시험용 고정 자료. 운영 이미지에 넣지 않는다.

사용:  uv run python tools/catalog/make_sample.py [--source <catalog_enriched.json>] [--per-category 2] [--out tests/fixtures/catalog_sample.json]
결과는 tests/fixtures/catalog_sample.json 에 커밋한다 — 실험 파일이 없는 환경(CI·팀원)에서도 같은 자료로 시험할 수 있게.
재생성해도 같은 상품이 나오도록 카테고리별 productId 오름차순으로 고른다(무작위 없음)."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from profiling.adapters.catalog_reader_file import FileCatalogReader

ap = argparse.ArgumentParser()
ap.add_argument("--source", default="../../../KTB4_12team/data/gift-catalog-20260915-v1/catalog_enriched.json")
ap.add_argument("--per-category", type=int, default=2)
ap.add_argument("--out", default="tests/fixtures/catalog_sample.json")
args = ap.parse_args()

_, products = FileCatalogReader(Path(args.source)).active()          # 원형 → 7.9 변환·검증은 adapter가 이미 한다
by_cat: dict[str, list] = defaultdict(list)
for p in sorted(products, key=lambda p: p.productId):
    if len(by_cat[p.categoryName]) < args.per_category:
        by_cat[p.categoryName].append(p)
picked = sorted((p for ps in by_cat.values() for p in ps), key=lambda p: p.productId)

# 시험에 필요한 경우를 일부러 섞는다: 설명 null 1건 · 판매 불가 2건 (DB 저장·조인·감점이 이 경우를 다루는지)
rows = [p.model_dump(mode="json") for p in picked]
rows[0]["description"] = None
for r in rows[1:3]:
    r["available"] = False
# 조회수 — Backend가 초기에 임의 값을 넣어 보내는 것과 같은 상황을 흉내. productId 기반 결정적 값(재생성해도 동일), 0~9999
for r in rows:
    r["viewCount"] = (r["productId"] * 2654435761) % 10000

doc = {"message": "상품 목록을 조회했습니다.", "data": {"generatedAt": datetime.now(UTC).isoformat(timespec="seconds"), "products": rows}}
out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{out}: 상품 {len(rows)}건 · 카테고리 {len(by_cat)}개 · 설명 null 1 · 판매 불가 2")
