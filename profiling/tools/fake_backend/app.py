"""페이크 Backend — Backend 역할 전부 + 시험 콘솔. 시험 전용, 운영 이미지에 넣지 않는다.

  7.7 수신   POST /api/internal/v1/recipients/{recipientUserId}/profile   ← AI 콜백을 받아 검증·기록
  7.9 제공   GET  /internal/v1/ai/products/export                          ← AI Catalog 빌드가 가져감 (tests/fixtures/catalog_sample.json)
  콘솔       GET  /console  · POST /console/send-7.6 (AI로 대신 보냄) · GET /console/health-ai · GET /console/catalog · PUT /console/mode

실행:  uv run uvicorn tools.fake_backend.app:app --port 8081        (profiling 앱은 8000)
환경:  AI_BASE_URL(기본 http://localhost:8000) · AI_SERVICE_TOKEN(기본 dev-token) · FAKE_BACKEND_CATALOG(기본 tests/fixtures/catalog_sample.json)
확인:  브라우저 http://localhost:8081/console  ·  curl localhost:8081/received

실패 주입 (개발 이슈 #23 재시도 시험용):  FAKE_BACKEND_MODE = ok(기본) | 409 | 400 | 500 | timeout
  409 → STALE_SOURCE_VERSION (AI는 재시도 없이 SUPERSEDED)   400 → INVALID_REQUEST (재시도 없음)
  500 → INTERNAL_SERVER_ERROR (AI는 최대 3회 재시도)         timeout → 응답 없이 오래 기다리게 함

검증 규칙 출처: 모델 API 설계 v3.2.7 §7.7
  - 경로 {recipientUserId} ≠ 본문 recipientUserId → 400 RECIPIENT_ID_MISMATCH
  - recommendedProductIds ≤30, profileStatus="COMPLETED", 태그 필드 없음 — ProfileCallbackRequest(extra="forbid")가 잡는다
  - 200 SuccessResponse[ProfileCallbackAccepted]  message="수신자 프로필이 성공적으로 저장되었습니다."
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Body, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from profiling.adapters.catalog_reader_file import FileCatalogReader
from profiling.profile.ports import NoActiveCatalog
from profiling.transport.schemas import (
    ErrorBody,
    ErrorResponse,
    ProductExportData,
    ProfileCallbackAccepted,
    ProfileCallbackRequest,
    SuccessResponse,
)

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s fake_backend: %(message)s")
log = logging.getLogger("fake_backend")

app = FastAPI(title="fake backend (7.7 · 시험용)")

MODE = os.environ.get("FAKE_BACKEND_MODE", "ok")
MODES = ("ok", "409", "400", "500", "timeout")
CALLBACK_PATH = "/api/internal/v1/recipients/{recipientUserId}/profile"
EXPORT_PATH = "/internal/v1/ai/products/export"                       # 문서 1 §7.9
EXTRACT_AND_POOL_PATH = "/api/internal/v1/ai/profile/extract-and-pool"  # 문서 1 §7.6 (AI 쪽 경로)

HERE = Path(__file__).resolve().parent
AI_BASE_URL = os.environ.get("AI_BASE_URL", "http://localhost:8000").rstrip("/")
AI_SERVICE_TOKEN = os.environ.get("AI_SERVICE_TOKEN", "dev-token")
CATALOG_FILE = Path(os.environ.get("FAKE_BACKEND_CATALOG", "tests/fixtures/catalog_sample.json"))

# 받은 콜백을 순서대로 보관 — /received 로 확인. 프로세스 메모리라 재시작하면 사라진다.
_received: list[dict] = []
_lock = threading.Lock()
# 수신자별 sourceVersion — 실제 Backend는 수신자의 비선호·취향·리뷰가 바뀔 때마다 이 값을 올려서 7.6에 싣는다.
# 시험 환경에는 Backend가 없으므로 fake가 흉내 낸다: 같은 수신자로 보낼 때마다 +1 (콘솔의 "자동 증가"가 켜져 있을 때).
_source_versions: dict[int, int] = {}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(message=message, error=ErrorBody(code=code)).model_dump())


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """본문이 ProfileCallbackRequest에 어긋남(태그 필드 포함·31개 이상·타입 오류) → 400 INVALID_REQUEST.
    AI 쪽 버그를 바로 보이게 어떤 필드가 문제인지 message에 남긴다."""
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    msg = f"7.7 본문 오류: {loc} — {first.get('msg', '')}"
    log.warning("400 %s", msg)
    return _error(400, "INVALID_REQUEST", msg)


@app.post(CALLBACK_PATH)
async def receive_profile_callback(
    recipientUserId: int,
    body: ProfileCallbackRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """7.7 수신. 검증 순서: 스키마(위 핸들러) → 경로·본문 ID 일치 → 실패 주입 모드 → 기록 → 200."""
    if body.recipientUserId != recipientUserId:
        log.warning("400 RECIPIENT_ID_MISMATCH path=%s body=%s", recipientUserId, body.recipientUserId)
        return _error(400, "RECIPIENT_ID_MISMATCH", f"경로 {recipientUserId} ≠ 본문 {body.recipientUserId}")

    # 실패 주입 — 오늘은 ok만 쓰고, 재시도 규칙(#23)을 만들 때 나머지를 켠다
    if MODE == "409":
        return _error(409, "STALE_SOURCE_VERSION", "더 새로운 sourceVersion이 이미 저장되어 있습니다.")
    if MODE == "400":
        return _error(400, "INVALID_REQUEST", "주입된 실패")
    if MODE == "500":
        return _error(500, "INTERNAL_SERVER_ERROR", "주입된 실패")
    if MODE == "timeout":
        await asyncio.sleep(60)

    record = {
        "receivedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "authorization": authorization,  # AI가 서비스 토큰을 붙였는지 눈으로 확인
        **body.model_dump(),
    }
    with _lock:
        _received.append(record)
    log.info("7.7 수신 recipient=%s source_version=%s ids=%d first=%s",
             body.recipientUserId, body.sourceVersion, len(body.recommendedProductIds), body.recommendedProductIds[:3])

    return JSONResponse(
        status_code=200,
        content=SuccessResponse(
            message="수신자 프로필이 성공적으로 저장되었습니다.",
            data=ProfileCallbackAccepted(recipientUserId=body.recipientUserId, sourceVersion=body.sourceVersion, profileStatus="COMPLETED"),
        ).model_dump(),
    )


@app.get("/received")
def received() -> dict:
    """지금까지 받은 콜백 전부 — E2E 확인용. 최신이 마지막."""
    with _lock:
        return {"mode": MODE, "count": len(_received), "items": list(_received)}


@app.delete("/received")
def clear() -> dict:
    """시험 사이에 비우기."""
    with _lock:
        n = len(_received)
        _received.clear()
    return {"cleared": n}


# ---------------------------------------------------------------------------
# 7.9 상품 전체 export — Backend → AI Catalog 빌드가 가져간다. 예시 카탈로그(7.9 형식·111건)를 그대로 돌려준다.
# ---------------------------------------------------------------------------


def _catalog_products() -> list[dict[str, Any]]:
    """FileCatalogReader로 읽어(검증 포함) 7.9 dict 목록으로. 파일이 바뀌면 다음 호출에 반영되도록 매번 읽는다(시험용이라 성능 무관)."""
    _, products = FileCatalogReader(CATALOG_FILE).active()
    return [p.model_dump(mode="json") for p in products]


@app.get(EXPORT_PATH)
def export_products(authorization: str | None = Header(default=None)) -> JSONResponse:
    """7.9. 서비스 토큰이 없으면 401 — AI Catalog 빌드가 토큰을 붙이는지 확인하는 용도."""
    if not authorization or not authorization.startswith("Bearer "):
        return _error(401, "UNAUTHORIZED", "서비스 토큰이 없습니다.")
    products = _catalog_products()
    data = ProductExportData(generatedAt=datetime.now(UTC), products=products)
    log.info("7.9 export 제공 products=%d", len(products))
    return JSONResponse(status_code=200, content=SuccessResponse(message="상품 목록을 조회했습니다.", data=data).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 시험 콘솔 — 브라우저는 fake_backend에만 붙고, 7.6은 fake_backend가 AI로 대신 보낸다 (CORS 없음 · 토큰은 서버에만)
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> RedirectResponse:
    """루트를 열면 콘솔로 — 브라우저에서 :8081 만 쳐도 화면이 보이게."""
    return RedirectResponse(url="/console")


@app.get("/console")
def console() -> FileResponse:
    return FileResponse(HERE / "static" / "console.html")


@app.get("/console/catalog")
def console_catalog() -> dict:
    """화면의 상품 검색·비선호 카테고리 토글용 요약 목록. 카탈로그를 못 읽으면 500 대신 이유를 돌려준다(화면에 표시)."""
    try:
        products = _catalog_products()
    except NoActiveCatalog as e:
        return {"file": str(CATALOG_FILE), "count": 0, "categories": [], "products": [], "error": str(e)}
    cats = sorted({(p["categoryId"], p["categoryName"]) for p in products}, key=lambda c: c[1])
    return {"file": str(CATALOG_FILE), "count": len(products),
            "categories": [{"categoryId": i, "categoryName": n} for i, n in cats],
            "products": [{k: p[k] for k in ("productId", "name", "brand", "categoryId", "categoryName", "available", "viewCount")} for p in products]}


@app.post("/console/send-7.6")
def console_send_extract(body: Annotated[dict, Body()], auto_source_version: bool = True) -> dict:
    """본문(7.6 JSON, 검증하지 않음 — 일부러 틀린 본문도 보내 보게)을 AI로 전달하고 응답을 그대로 돌려준다.

    auto_source_version=True(기본)면 Backend처럼 수신자별 sourceVersion을 fake가 관리한다: 마지막 값 + 1을 본문에 덮어쓴다
    (처음 보는 수신자는 본문 값 또는 1부터). False면 본문의 sourceVersion을 그대로 보낸다 — 순서 역전(409) 시험용.
    """
    t0 = time.perf_counter()
    rid = body.get("recipientUserId")
    if auto_source_version and isinstance(rid, int):
        with _lock:
            base = _source_versions.get(rid)
            nxt = (base + 1) if base is not None else max(int(body.get("sourceVersion") or 1), 1)
            _source_versions[rid] = nxt
        body = {**body, "sourceVersion": nxt}
    try:
        with httpx.Client(base_url=AI_BASE_URL, timeout=10) as cli:
            res = cli.post(EXTRACT_AND_POOL_PATH, json=body, headers={"Authorization": f"Bearer {AI_SERVICE_TOKEN}"})
    except httpx.RequestError as e:
        return {"ok": False, "status": None, "error": f"AI 연결 실패 ({AI_BASE_URL}): {type(e).__name__}: {e}", "elapsedMs": round((time.perf_counter() - t0) * 1000)}
    try:
        payload = res.json()
    except ValueError:
        payload = res.text
    log.info("콘솔 7.6 전달 recipient=%s sourceVersion=%s → %s", rid, body.get("sourceVersion"), res.status_code)
    return {"ok": res.status_code == 202, "status": res.status_code, "body": payload, "elapsedMs": round((time.perf_counter() - t0) * 1000),
            "sentSourceVersion": body.get("sourceVersion"), "sent": {"url": f"{AI_BASE_URL}{EXTRACT_AND_POOL_PATH}", "authorization": "Bearer ***"}}


@app.get("/console/source-versions")
def console_source_versions() -> dict:
    """fake가 기억하는 수신자별 sourceVersion (Backend 흉내)."""
    with _lock:
        return {"versions": dict(_source_versions)}


@app.delete("/console/source-versions")
def console_source_versions_reset() -> dict:
    with _lock:
        n = len(_source_versions); _source_versions.clear()
    return {"cleared": n}


@app.get("/console/health-ai")
def console_health_ai() -> dict:
    try:
        with httpx.Client(base_url=AI_BASE_URL, timeout=3) as cli:
            res = cli.get("/health")
        return {"ok": res.status_code == 200, "status": res.status_code, "body": res.json(), "url": AI_BASE_URL}
    except (httpx.RequestError, ValueError) as e:
        return {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}", "url": AI_BASE_URL}


@app.get("/console/mode")
def console_mode_get() -> dict:
    return {"mode": MODE, "modes": MODES}


@app.put("/console/mode")
def console_mode_put(body: Annotated[dict, Body()]) -> dict:
    """실패 주입 모드를 실행 중에 바꾼다 (재시작 없이 409·500·timeout 갈래를 시험)."""
    global MODE
    mode = str(body.get("mode", "ok"))
    if mode not in MODES:
        return {"mode": MODE, "error": f"mode는 {MODES} 중 하나"}
    MODE = mode
    log.info("콘솔: 실패 주입 모드 = %s", MODE)
    return {"mode": MODE, "modes": MODES}
