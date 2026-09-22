"""7.9 상품 export 가져오기 + 계약 점검 + 상품 ID 대조 — AI Catalog CLI (문서 1 §7.9 "개발자 수동 실행").

무엇을 하나
  1) Backend의 GET /internal/v1/ai/products/export 를 Bearer 토큰으로 호출한다 (또는 --from-file 로 저장된 JSON을 읽는다)
  2) 계약 점검: 봉투 모양 · 상품마다 필수 필드·타입(ProductRecord) · 계약에 없는 필드 이름과 건수 · 조회수 필드 이름
     → 필수 누락·타입 오류가 하나라도 있으면 ErrorCode.CONTRACT_7_9_SCHEMA 로 보고하고 저장하지 않는다 (종료 코드 1)
  3) ID 대조: 지금 AI가 쓰는 카탈로그(--compare, 기본 settings.CATALOG_FILE)와 비교 — 한쪽에만 있는 productId · 같은 ID의
     카테고리/판매 여부/이름 변경 → 표로 출력. 차이가 있으면 종료 코드 2 (저장은 한다)
     --compare-db 를 주면 팀원 검색 테이블 ai_search.products(product_id TEXT)와도 대조한다 — 숫자로 못 바꾸는 ID(예: 원형 "kakao:123")는
     Backend BIGINT id와 맞출 수 없으므로 따로 센다
  4) --out 에 7.9 봉투 형식으로 저장 (FileCatalogReader가 그대로 읽는 형식). .env 의 PROFILING_CATALOG_FILE 을 이 경로로 바꾸면 적용

쓰는 법 (profiling/ 에서)
  uv run python -m tools.catalog.fetch_export --out data/catalog_export.json                 # BACKEND_BASE_URL·SERVICE_TOKEN 은 설정에서
  uv run python -m tools.catalog.fetch_export --base-url http://localhost:8081 --token dev-token --out data/catalog_export.json
  uv run python -m tools.catalog.fetch_export --from-file export.json --compare tests/fixtures/catalog_sample.json --dry-run
  uv run python -m tools.catalog.fetch_export --from-file export.json --compare none --compare-db --dry-run   # AI DB 테이블과만

종료 코드: 0 이상 없음 · 1 계약 위반(저장 안 함) · 2 ID/내용 차이 있음(저장함) · 3 연결·파일 오류
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from pydantic import ValidationError

from profiling.adapters.catalog_reader_file import FileCatalogReader
from profiling.config.settings import get_settings
from profiling.profile.ports import NoActiveCatalog
from profiling.profile.types import ErrorCode
from profiling.transport.schemas import (
    PRODUCT_FIELD_ALIASES,
    PRODUCT_FIELDS,
    ProductRecord,
)

EXPORT_PATH = "/internal/v1/ai/products/export"
REQUIRED_FIELDS = frozenset(n for n, f in ProductRecord.model_fields.items() if f.is_required())
_VIEW_NAMES = ("viewCount", "views", "view_count")


# ---------------------------------------------------------------------------
# 1) 가져오기
# ---------------------------------------------------------------------------


def fetch(base_url: str, token: str, timeout_s: float = 60.0) -> Any:
    """GET 7.9 → 응답 JSON. HTTP 오류·비JSON은 RuntimeError."""
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s) as cli:
        res = cli.get(EXPORT_PATH, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if res.status_code != 200:
        raise RuntimeError(f"7.9 응답 {res.status_code}: {res.text[:200]}")
    try:
        doc = res.json()
    except ValueError as e:
        raise RuntimeError(f"7.9 응답이 JSON이 아님: {res.text[:200]}") from e
    return doc          # 봉투 모양은 check_contract()가 판정한다


# ---------------------------------------------------------------------------
# 2) 계약 점검
# ---------------------------------------------------------------------------


@dataclass
class ContractReport:
    total: int = 0
    valid: list[ProductRecord] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)          # (index, 첫 오류 한 줄)
    unknown_fields: Counter = field(default_factory=Counter)               # 계약에 없는 필드 이름 → 등장 건수
    missing_fields: Counter = field(default_factory=Counter)               # 필수인데 없는 필드 이름 → 건수
    view_field: str | None = None                                          # 조회수가 어떤 이름으로 왔나 (없으면 None)
    generated_at: str | None = None
    envelope_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.envelope_error is None and not self.invalid

    @property
    def code(self) -> ErrorCode | None:
        return None if self.ok else ErrorCode.CONTRACT_7_9_SCHEMA


def _products_of(doc: Any) -> tuple[list[Any] | None, str | None, str | None]:
    """봉투에서 products 목록·generatedAt을 꺼낸다. (products, generatedAt, 봉투 오류)"""
    if isinstance(doc, list):                                # 봉투 없이 목록만 온 경우도 받아 준다
        return doc, None, None
    if not isinstance(doc, dict):
        return None, None, "최상위가 객체/배열이 아님"
    data = doc.get("data", doc)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return None, None, "data.products 배열이 없음 (SuccessResponse<ProductExportData> 봉투가 아님)"
    return data["products"], data.get("generatedAt"), None


def check_contract(doc: Any) -> ContractReport:
    """7.9 응답 전체를 ProductRecord 계약과 대조한다. 상품을 버리지 않고 무엇이 어떻게 안 맞는지 센다."""
    rep = ContractReport()
    products, generated_at, err = _products_of(doc)
    if err:
        rep.envelope_error = err
        return rep
    rep.generated_at = generated_at
    rep.total = len(products)
    for i, raw in enumerate(products):
        if not isinstance(raw, dict):
            rep.invalid.append((i, "상품이 객체가 아님"))
            continue
        keys = set(raw)
        for k in keys - PRODUCT_FIELDS - PRODUCT_FIELD_ALIASES:
            rep.unknown_fields[k] += 1
        for k in REQUIRED_FIELDS - keys:
            rep.missing_fields[k] += 1
        if rep.view_field is None:
            rep.view_field = next((n for n in _VIEW_NAMES if n in keys), None)
        try:
            rep.valid.append(ProductRecord.model_validate(raw))
        except ValidationError as e:
            first = e.errors()[0]
            rep.invalid.append((i, f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"))
    return rep


# ---------------------------------------------------------------------------
# 3) ID 대조
# ---------------------------------------------------------------------------


@dataclass
class DiffReport:
    only_in_backend: list[int] = field(default_factory=list)
    only_in_ai: list[int] = field(default_factory=list)
    changed: list[tuple[int, str, Any, Any]] = field(default_factory=list)   # (productId, 필드, AI 값, Backend 값)
    common: int = 0

    @property
    def clean(self) -> bool:
        return not (self.only_in_backend or self.only_in_ai or self.changed)


def compare(ai_products: list[ProductRecord], backend_products: list[ProductRecord]) -> DiffReport:
    """지금 AI가 가진 상품과 Backend export를 productId로 맞춰 본다. 7.7로 돌려줄 ID가 Backend에 있는지가 핵심."""
    a = {p.productId: p for p in ai_products}
    b = {p.productId: p for p in backend_products}
    rep = DiffReport(only_in_backend=sorted(b.keys() - a.keys()), only_in_ai=sorted(a.keys() - b.keys()))
    for pid in sorted(a.keys() & b.keys()):
        rep.common += 1
        for f in ("categoryId", "categoryName", "available", "name"):
            if getattr(a[pid], f) != getattr(b[pid], f):
                rep.changed.append((pid, f, getattr(a[pid], f), getattr(b[pid], f)))
    return rep


@dataclass
class DbDiffReport:
    """ai_search.products(TEXT id) vs Backend export(BIGINT id)."""
    table_missing: bool = False
    total: int = 0
    non_numeric_ids: list[str] = field(default_factory=list)     # int로 못 바꾸는 product_id — Backend id와 대조 불가
    only_in_backend: list[int] = field(default_factory=list)
    only_in_ai: list[int] = field(default_factory=list)
    name_changed: list[tuple[int, str, str]] = field(default_factory=list)
    common: int = 0

    @property
    def clean(self) -> bool:
        return not (self.table_missing or self.non_numeric_ids or self.only_in_backend or self.only_in_ai or self.name_changed)


def compare_db(engine: sa.Engine, backend_products: list[ProductRecord]) -> DbDiffReport:
    """팀원 검색 테이블 ai_search.products 와 대조. product_id가 TEXT라 숫자 변환이 되는 것만 Backend id와 맞춘다."""
    rep = DbDiffReport()
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("select product_id, name from ai_search.products")).all()
    except sa.exc.ProgrammingError:          # 테이블·스키마 없음
        rep.table_missing = True
        return rep
    rep.total = len(rows)
    ai: dict[int, str] = {}
    for pid, name in rows:
        text = str(pid)
        if text.isdigit():                   # 순수 숫자만 Backend id 후보. "kakao:1234" 같은 원형 ID는 뒤 숫자를 떼어 맞추지 않는다 —
            ai[int(text)] = name             # 그 숫자는 수집처 번호이지 Backend auto-increment id가 아니라서 우연히 겹치면 오판한다
        else:
            rep.non_numeric_ids.append(text)
    b = {p.productId: p for p in backend_products}
    rep.only_in_backend = sorted(b.keys() - ai.keys())
    rep.only_in_ai = sorted(ai.keys() - b.keys())
    for pid in sorted(ai.keys() & b.keys()):
        rep.common += 1
        if ai[pid] != b[pid].name:
            rep.name_changed.append((pid, ai[pid], b[pid].name))
    return rep


# ---------------------------------------------------------------------------
# 4) 저장 · 출력
# ---------------------------------------------------------------------------


def to_document(rep: ContractReport, source: str) -> dict[str, Any]:
    """저장용 7.9 봉투 — 검증을 통과한 상품을 계약 이름(viewCount 등)으로 정규화해서 담는다."""
    return {
        "message": "상품 목록을 조회했습니다.",
        "data": {"generatedAt": rep.generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
                 "products": [p.model_dump(mode="json") for p in rep.valid]},
        "fetchedFrom": source, "fetchedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _fmt_ids(ids: list[int], n: int = 10) -> str:
    return ", ".join(map(str, ids[:n])) + (f" … (+{len(ids) - n})" if len(ids) > n else "")


def print_db_report(d: DbDiffReport, target: str) -> None:
    print(f"== ID 대조 (AI DB ai_search.products @ {target}  vs  Backend export)")
    if d.table_missing:
        print("  (테이블 없음 — 팀원 검색 테이블이 이 DB에 아직 없음. 생기면 다시)")
        return
    print(f"  AI DB {d.total}건 · 양쪽에 있음 {d.common} · Backend에만 {len(d.only_in_backend)} · AI에만 {len(d.only_in_ai)} · 이름 다름 {len(d.name_changed)}")
    if d.non_numeric_ids:
        print(f"  ✗ 숫자가 아닌 product_id {len(d.non_numeric_ids)}건 — Backend BIGINT id와 맞출 수 없음: {', '.join(d.non_numeric_ids[:5])}"
              + (" …" if len(d.non_numeric_ids) > 5 else ""))
    if d.only_in_backend:
        print(f"  Backend에만: {_fmt_ids(d.only_in_backend)}")
    if d.only_in_ai:
        print(f"  AI에만:      {_fmt_ids(d.only_in_ai)}  (7.7로 보내면 Backend 화면에 안 뜸)")
    for pid, a, b in d.name_changed[:5]:
        print(f"  이름 다름 {pid}: AI {a!r} / Backend {b!r}")
    if d.common and len(d.name_changed) * 2 > d.common:
        print(f"  ✗ 같은 번호인데 이름이 다른 상품이 {len(d.name_changed)}/{d.common} — ID 체계 자체가 다를 가능성(수집처 번호 vs Backend id). 7.9 export로 다시 적재해야 함")
    if d.clean:
        print("  ✓ 상품 ID 일치")


def print_report(rep: ContractReport, diff: DiffReport | None, compare_label: str | None) -> None:
    print("== 계약 점검 (7.9 → ProductRecord)")
    if rep.envelope_error:
        print(f"  ✗ {ErrorCode.CONTRACT_7_9_SCHEMA}: {rep.envelope_error}")
        return
    print(f"  상품 {rep.total}건 · 유효 {len(rep.valid)} · 계약 위반 {len(rep.invalid)} · generatedAt {rep.generated_at or '-'}")
    print(f"  조회수 필드: {rep.view_field or '없음(전부 0으로 처리 — v1 정렬이 productId 순이 됨)'}")
    if rep.unknown_fields:
        print("  계약에 없는 필드(무시함): " + ", ".join(f"{k}×{v}" for k, v in rep.unknown_fields.most_common()))
    if rep.missing_fields:
        print("  필수인데 빠진 필드: " + ", ".join(f"{k}×{v}" for k, v in rep.missing_fields.most_common()))
    for i, msg in rep.invalid[:5]:
        print(f"  ✗ products[{i}] {msg}")
    if len(rep.invalid) > 5:
        print(f"  … 위반 {len(rep.invalid) - 5}건 더")
    if not rep.ok:
        print(f"  → {ErrorCode.CONTRACT_7_9_SCHEMA}: 필수 누락·타입 오류가 있어 저장하지 않음. Backend 필드 이름·타입을 맞춘 뒤 다시.")
    if diff is not None:
        print(f"== ID 대조 (AI: {compare_label}  vs  Backend export)")
        print(f"  양쪽에 있음 {diff.common} · Backend에만 {len(diff.only_in_backend)} · AI에만 {len(diff.only_in_ai)} · 내용 바뀜 {len(diff.changed)}")
        if diff.only_in_backend:
            print(f"  Backend에만: {_fmt_ids(diff.only_in_backend)}  (AI가 아직 모르는 상품 — 저장하면 해결)")
        if diff.only_in_ai:
            print(f"  AI에만:      {_fmt_ids(diff.only_in_ai)}  (7.7로 이 ID를 보내면 Backend 화면에 안 뜸)")
        for pid, f, old, new in diff.changed[:10]:
            print(f"  변경 {pid} {f}: {old!r} → {new!r}")
        if len(diff.changed) > 10:
            print(f"  … 변경 {len(diff.changed) - 10}건 더")
        if diff.clean:
            print("  ✓ 상품 ID·카테고리·판매 여부 일치")


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="7.9 export 가져오기 · 계약 점검 · 상품 ID 대조")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--base-url", default=settings.BACKEND_BASE_URL, help=f"Backend 주소 (기본 settings.BACKEND_BASE_URL={settings.BACKEND_BASE_URL})")
    src.add_argument("--from-file", type=Path, help="HTTP 대신 저장된 7.9 JSON 파일을 읽는다")
    ap.add_argument("--token", default=settings.SERVICE_TOKEN, help="Bearer 토큰 (기본 settings.SERVICE_TOKEN)")
    ap.add_argument("--out", type=Path, default=Path("data/catalog_export.json"), help="저장 경로 (기본 data/catalog_export.json)")
    ap.add_argument("--compare", type=Path, default=settings.CATALOG_FILE, help=f"대조할 AI 카탈로그 파일 (기본 settings.CATALOG_FILE={settings.CATALOG_FILE}) · 'none'이면 생략")
    ap.add_argument("--compare-db", action="store_true", help="settings.DATABASE_URL 의 ai_search.products(팀원 검색 테이블)와도 대조")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 점검·대조만")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    # 1) 가져오기
    try:
        if args.from_file:
            doc = json.loads(args.from_file.read_text(encoding="utf-8"))
            source = str(args.from_file)
        else:
            doc = fetch(args.base_url, args.token, args.timeout)
            source = f"{args.base_url.rstrip('/')}{EXPORT_PATH}"
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as e:
        print(f"✗ 가져오기 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    print(f"출처: {source}")

    # 2) 계약 점검
    rep = check_contract(doc)

    # 3) 대조 (AI 현재 카탈로그가 있을 때만)
    diff = None
    compare_label = None
    if rep.ok and str(args.compare).lower() != "none":
        try:
            current = FileCatalogReader(args.compare)
            _, ai_products = current.active()
            diff = compare(ai_products, rep.valid)
            compare_label = str(args.compare)
        except NoActiveCatalog as e:
            print(f"(대조 생략 — AI 카탈로그를 읽지 못함: {e})")

    print_report(rep, diff, compare_label)
    if not rep.ok:
        return 1
    db_diff = None
    if args.compare_db:
        try:
            db_diff = compare_db(sa.create_engine(settings.DATABASE_URL, future=True), rep.valid)
            print_db_report(db_diff, settings.DATABASE_URL.split("@")[-1])
        except sa.exc.OperationalError as e:
            print(f"(AI DB 대조 생략 — 연결 실패: {type(e).__name__})")

    # 4) 저장
    if args.dry_run:
        print(f"(dry-run — 저장하지 않음. 저장하면: {args.out})")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(to_document(rep, source), ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"저장: {args.out} ({len(rep.valid)}건) → .env 에 PROFILING_CATALOG_FILE={args.out} 로 지정하고 AI 앱 재시작")
    clean = (diff is None or diff.clean) and (db_diff is None or db_diff.clean)
    return 0 if clean else 2


if __name__ == "__main__":
    sys.exit(main())
