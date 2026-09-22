"""Catalog adapter (파일) — 카탈로그 JSON 한 개를 "활성 카탈로그 1버전"으로 제공한다. DB adapter(catalog_versions·products)가 생기면 삭제.

읽을 수 있는 형식 두 가지 (자동 판별):
  (a) 7.9 export 형식 — `{"message", "data": {"generatedAt", "products": [ProductRecord…]}}` 또는 `{"generatedAt", "products"}` 또는 `[ProductRecord…]`
  (b) 동료 공유본 원형 — `{"schema_version", "products": [{product_id: "KAKAO_GIFT:123", name, normalized_name, brand, description,
      category, category_id: "CAT-01-02", price_krw, sale_status, sold_out, …}]}`  → 실험 `common.adapt_catalog()`와 같은 규칙으로 7.9 형식으로 변환

검증: 필수 필드·타입은 ProductRecord(pydantic)가 잡는다. 실패한 상품은 버리고 개수를 로그로. 중복 productId는 뒤의 것을 버린다.
전부 실패하거나 파일이 없으면 NoActiveCatalog — main.lifespan이 잡아서 7.6이 503을 내게 한다.

DB adapter로 바꿀 때 유지할 계약(ports.CatalogReader): active() → (catalog_version_id, list[ProductRecord]) · by_id(product_id) → ProductRecord | None.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from profiling.ports import NoActiveCatalog
from profiling.schemas import ProductRecord
from profiling.types import ErrorCode

log = logging.getLogger(__name__)

# 파일 카탈로그는 버전이 하나뿐이다. DB에서는 catalog_versions.id 가 이 자리에 온다.
FILE_CATALOG_VERSION_ID = UUID("00000000-0000-0000-0000-000000000001")   # 파일 카탈로그 고정 버전. DB 카탈로그는 catalog_versions.id(uuid)

# 동료 공유본에 fetched_at이 없을 때 쓰는 updatedAt (실험 하네스와 같은 값)
_RAW_DEFAULT_UPDATED_AT = "2026-09-15T00:00:00Z"
_RAW_CATEGORY_ID = re.compile(r"CAT-(\d+)-(\d+)")


def _is_raw_format(doc: Any) -> bool:
    """동료 공유본 원형인가 — products[0]에 price_krw가 있으면."""
    return isinstance(doc, dict) and isinstance(doc.get("products"), list) and bool(doc["products"]) and "price_krw" in doc["products"][0]


def _adapt_raw_product(p: dict[str, Any]) -> dict[str, Any]:
    """동료 공유본 상품 1건 → 7.9 필드 dict. 실험 `common.adapt_catalog()`와 같은 규칙 (categoryId = 대분류*100 + 소분류)."""
    pid = int(str(p["product_id"]).split(":")[-1])
    m = _RAW_CATEGORY_ID.match(str(p.get("category_id", "")))
    cid = int(m.group(1)) * 100 + int(m.group(2)) if m else abs(hash(p["category"])) % 10000
    return {
        "productId": pid,
        "name": p.get("normalized_name") or p["name"],
        "brand": p["brand"],
        "description": p.get("description") or None,
        "categoryId": cid,
        "categoryName": p["category"],
        "price": int(p["price_krw"]),
        "available": (p.get("sale_status") == "ON_SALE") and not p.get("sold_out", False),
        "updatedAt": (p.get("provenance") or {}).get("fetched_at") or _RAW_DEFAULT_UPDATED_AT,
    }


def _product_dicts(doc: Any) -> list[dict[str, Any] | None]:
    """어떤 형식이든 7.9 필드 dict 목록으로. 원형 변환에 실패한 상품(필수 키 없음 등)은 None — 호출자가 형식 오류로 센다."""
    if _is_raw_format(doc):
        out: list[dict[str, Any] | None] = []
        for p in doc["products"]:
            try:
                out.append(_adapt_raw_product(p))
            except (KeyError, TypeError, ValueError):
                out.append(None)
        return out
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        inner = doc.get("data", doc)
        if isinstance(inner, dict) and isinstance(inner.get("products"), list):
            return inner["products"]
    raise NoActiveCatalog("카탈로그 파일 형식을 알 수 없습니다 (7.9 export 또는 동료 공유본이어야 함)")


class FileCatalogReader:
    """ports.CatalogReader 구현 — 시작 시 파일을 1회 읽어 메모리에 보관. 이후 active()·by_id()는 메모리에서."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise NoActiveCatalog(f"카탈로그 파일이 없습니다: {self.path}")
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise NoActiveCatalog(f"카탈로그 파일을 읽지 못했습니다: {self.path} ({e})") from e

        products: list[ProductRecord] = []
        seen: set[int] = set()
        n_invalid = n_dup = 0
        for raw in _product_dicts(doc):
            if raw is None:
                n_invalid += 1
                continue
            try:
                rec = ProductRecord.model_validate(raw)
            except ValidationError:
                n_invalid += 1
                continue
            if rec.productId in seen:
                n_dup += 1
                continue
            seen.add(rec.productId)
            products.append(rec)

        if not products:
            raise NoActiveCatalog(f"유효한 상품이 없습니다: {self.path} (형식 오류 {n_invalid}건)")

        self._products = products
        self._by_id = {p.productId: p for p in products}
        self.loaded_at = datetime.now(UTC)
        self.version_label = str(doc.get("schema_version") or doc.get("category_version") or "") if isinstance(doc, dict) else ""
        log.info("FileCatalogReader 로드 %s: 상품 %d건 (형식 오류 %d · 중복 %d · 판매중 %d) label=%s",
                 self.path.name, len(products), n_invalid, n_dup, sum(p.available for p in products), self.version_label or "-")
        if n_invalid:
            log.warning("%s %s: 상품 %d건이 7.9 필드 계약에 안 맞아 버림 — tools/catalog/fetch_export.py 로 어느 필드인지 확인",
                        ErrorCode.CONTRACT_7_9_SCHEMA, self.path.name, n_invalid)

    # ---- ports.CatalogReader ------------------------------------------------

    def active(self) -> tuple[int, list[ProductRecord]]:
        """(FILE_CATALOG_VERSION_ID, 상품 전체). 목록은 공유 객체이므로 호출자가 수정하지 않는다."""
        return FILE_CATALOG_VERSION_ID, self._products

    def by_id(self, product_id: int) -> ProductRecord | None:
        return self._by_id.get(product_id)

    # ---- 편의 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._products)
