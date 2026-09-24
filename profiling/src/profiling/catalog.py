"""Catalog adapter — 활성 카탈로그 1버전을 `ports.CatalogReader` 모양으로 제공한다. 구현 두 가지.

  DbCatalogReader   `ai_catalog`(마이그레이션 0003)를 읽는다. **배포는 이쪽이다** — 컨테이너 안에 카탈로그 파일이 없기 때문.
                    활성 버전은 `catalog_versions.is_active` 한 행이고, 그 `id`(uuid)가 그대로 `SearchResult.catalog_version_id`가 된다.
  FileCatalogReader 카탈로그 JSON 한 개. 로컬 개발·시험용으로 남긴다(`PROFILING_CATALOG_SOURCE=file`).

어느 쪽을 쓸지는 `main.lifespan`이 설정(`CATALOG_SOURCE`) 하나로 고른다. 업무 코드(pipeline)는 어느 쪽인지 모른다.

**상품 번호 규칙** (DbCatalogReader). 7.6·7.7은 Backend가 발급한 정수 번호로 말한다. 그 번호는 아직 없다(`backend_product_id` 전건 NULL).
그래서 없는 동안에는 수집처 ID의 숫자부(`KAKAO_GIFT:10002797` → `10002797`)를 **임시 번호**로 쓰고, `provisional_ids=True`로 표시해
기동 로그와 `/health`에 남긴다. Backend 회신을 `--id-map`으로 채우면 같은 코드가 진짜 번호를 쓰기 시작하고 표시는 사라진다.
임시 번호는 Backend에 없는 번호이므로 **7.7로 내보내면 화면에 상품이 뜨지 않는다** — 배포는 되지만 추천이 맞으려면 회신이 먼저다.

--- 아래는 FileCatalogReader ---

파일 한 개를 "활성 카탈로그 1버전"으로 제공한다.

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
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import Engine

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


def _availability(p: dict[str, Any]) -> str:
    """동료 공유본의 판매 상태 → 3값. 상태를 모르면 unknown — 임의로 판매중이라고 하지 않는다."""
    if p.get("sold_out"):
        return "unavailable"
    status = p.get("sale_status")
    if status is None:
        return "unknown"
    return "available" if status == "ON_SALE" else "unavailable"


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
        "availability": _availability(p),
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
        log.info("FileCatalogReader 로드 %s: 상품 %d건 (형식 오류 %d · 중복 %d · 재고 available %d · unknown %d) label=%s",
                 self.path.name, len(products), n_invalid, n_dup,
                 sum(p.availability == "available" for p in products), sum(p.availability == "unknown" for p in products),
                 self.version_label or "-")
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


# ===========================================================================
# DB 구현 — ai_catalog (마이그레이션 0003). 배포 기본값.
# ===========================================================================

SCHEMA = "ai_catalog"

_ACTIVE_VERSION = sa.text(f"select id, package_id from {SCHEMA}.catalog_versions where is_active")

## 활성 버전의 **패키지에 속한** 상품만. products 에는 버전 FK가 없고 package_id 만 있다 —
## 다른 패키지(예: 시험용)의 행이 섞여 있어도 활성 버전의 상품만 본다.
_PRODUCTS = sa.text(f"""
    select p.source_product_id, p.backend_product_id, p.name, p.brand, p.description,
           p.source_category_id, c.backend_category_id, c.name as category_name,
           p.unit_price, p.availability, p.view_count, p.updated_at
    from {SCHEMA}.products p
    join {SCHEMA}.categories c on c.source_category_id = p.source_category_id
    where p.package_id = :package_id""")

_SOURCE_PRODUCT_ID = re.compile(r"^[A-Z_]+:(\d+)$")        # KAKAO_GIFT:10002797
_SOURCE_CATEGORY_ID = re.compile(r"^CAT-(\d+)-(\d+)$")     # CAT-01-02


def _provisional_product_id(source_product_id: str) -> int:
    """Backend 번호가 없는 동안 쓰는 임시 상품 번호 — 수집처 ID의 숫자부. FileCatalogReader의 원형 변환과 같은 규칙."""
    m = _SOURCE_PRODUCT_ID.match(source_product_id)
    if not m:
        raise NoActiveCatalog(f"상품 ID에서 번호를 뽑을 수 없습니다: {source_product_id}")
    return int(m.group(1))


def _provisional_category_id(source_category_id: str) -> int:
    """임시 카테고리 번호 — 대분류*100 + 소분류. FileCatalogReader의 원형 변환과 같은 규칙."""
    m = _SOURCE_CATEGORY_ID.match(source_category_id)
    if not m:
        raise NoActiveCatalog(f"카테고리 ID에서 번호를 뽑을 수 없습니다: {source_category_id}")
    return int(m.group(1)) * 100 + int(m.group(2))


class DbCatalogReader:
    """ports.CatalogReader 구현 (PostgreSQL `ai_catalog`).

    `active()`는 호출마다 **활성 버전 id만** 한 번 묻고(작은 질의 한 개), 그 값이 지난번과 같으면 들고 있던 목록을 그대로 준다.
    달라졌을 때만 상품 전체를 다시 읽는다 — 카탈로그를 새로 적재해도 앱을 재시작할 필요가 없고, 요청마다 4천 건을 읽지도 않는다.
    백그라운드 스레드와 요청 스레드가 같이 부르므로 다시 읽는 구간은 락으로 묶는다.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._version: UUID | None = None
        self._products: list[ProductRecord] = []
        self._by_id: dict[int, ProductRecord] = {}
        self.provisional_ids = False
        self.loaded_at: datetime | None = None

    # ---- ports.CatalogReader ------------------------------------------------

    def active(self) -> tuple[UUID, list[ProductRecord]]:
        version, package_id = self._active_version()
        if version != self._version:
            with self._lock:
                if version != self._version:          # 락을 기다리는 동안 다른 스레드가 이미 읽었을 수 있다
                    self._load(version, package_id)
        return version, self._products

    def by_id(self, product_id: int) -> ProductRecord | None:
        self.active()                                  # 버전이 바뀌었으면 먼저 따라잡는다
        return self._by_id.get(product_id)

    # ---- 내부 ---------------------------------------------------------------

    def _active_version(self) -> tuple[UUID, str]:
        """(활성 버전 id, 그 패키지 이름). 작은 질의 하나 — active() 가 호출마다 부른다."""
        try:
            with self._engine.connect() as conn:
                row = conn.execute(_ACTIVE_VERSION).first()
        except sa.exc.SQLAlchemyError as e:
            raise NoActiveCatalog(f"카탈로그를 읽을 수 없습니다: {type(e).__name__}: {e}") from e
        if row is None:
            raise NoActiveCatalog(f"{SCHEMA}.catalog_versions 에 활성 버전이 없습니다 — tools/catalog/load_catalog.py 로 적재하세요")
        return row[0], row[1]                          # 부분 유니크 인덱스가 활성 1개를 보장한다

    def _load(self, version: UUID, package_id: str) -> None:
        with self._engine.connect() as conn:
            rows = conn.execute(_PRODUCTS, {"package_id": package_id}).mappings().all()
        if not rows:
            raise NoActiveCatalog(f"활성 버전 {version}(패키지 {package_id}) 에 상품이 없습니다")

        products: list[ProductRecord] = []
        provisional_p = provisional_c = 0
        for r in rows:
            pid = r["backend_product_id"]
            if pid is None:
                pid = _provisional_product_id(r["source_product_id"])
                provisional_p += 1
            cid = r["backend_category_id"]
            if cid is None:
                cid = _provisional_category_id(r["source_category_id"])
                provisional_c += 1
            products.append(ProductRecord(
                productId=pid, name=r["name"], brand=r["brand"], description=r["description"] or None,
                categoryId=cid, categoryName=r["category_name"], price=r["unit_price"],
                availability=r["availability"], updatedAt=r["updated_at"], viewCount=r["view_count"] or 0,
            ))

        products.sort(key=lambda p: p.productId)        # 7.9 export와 같은 순서
        by_id = {p.productId: p for p in products}
        if len(by_id) != len(products):                 # 진짜 번호와 임시 번호가 섞여 충돌한 경우 — 그대로 쓰면 다른 상품을 추천한다
            raise NoActiveCatalog(f"상품 번호가 겹칩니다({len(products) - len(by_id)}건) — Backend ID 회신을 끝까지 적용하세요")

        self._products, self._by_id, self._version = products, by_id, version
        self.provisional_ids = bool(provisional_p or provisional_c)
        self.loaded_at = datetime.now(UTC)
        log.info("DbCatalogReader 로드 version=%s package=%s 상품 %d건 (재고 available %d · unknown %d)",
                 version, package_id, len(products),
                 sum(p.availability == "available" for p in products), sum(p.availability == "unknown" for p in products))
        if self.provisional_ids:
            log.warning("%s 임시 상품 번호 사용 중 — 상품 %d/%d · 카테고리 %d건이 Backend 번호가 아니다. "
                        "7.7로 내보내면 Backend에 없는 번호가 된다. load_catalog.py --id-map 으로 회신을 채우세요",
                        ErrorCode.CONTRACT_7_9_SCHEMA, provisional_p, len(products), provisional_c)

    # ---- 편의 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._products)
