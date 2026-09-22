"""Backend adapter — Backend 정본으로 나가는 HTTP는 전부 여기. 오늘은 7.7 콜백 송신, 나중에 7.9 export 호출.

7.7 (모델 API 설계 v3.2.7 §7.7)
  POST {base_url}/api/internal/v1/recipients/{recipientUserId}/profile
  헤더  Authorization: Bearer <service_token>
  본문  ProfileCallbackRequest {recipientUserId, sourceVersion, profileStatus="COMPLETED", recommendedProductIds ≤30}  — 태그 없음(DR-035)

반환은 "실행 기록이 다음에 어떤 상태가 되는가"(types.RunStatus, 3단계 §10.2)로 돌려준다. 새 타입을 만들지 않는다.
  200                       → DELIVERED     전달 완료
  409 STALE_SOURCE_VERSION  → SUPERSEDED    더 새 버전이 이미 저장됨. 재시도 없음, 결과 폐기 (정상 경로)
  400 · 401 · 403           → FAILED        우리 쪽 버그·토큰. 재시도 없음
  5xx · 타임아웃 · 연결 실패   → RESULT_READY  아직 전달 못 함 — 재시도 대상 (오늘은 1회만 보내고 여기서 멈춘다. 재시도·백오프는 #23)
예외를 밖으로 내지 않는다 — 백그라운드에서 죽으면 조용히 사라지므로 반드시 상태로 돌려주고 로그를 남긴다.
"""

from __future__ import annotations

import logging
import time

import httpx
from pydantic import ValidationError

from profiling.profile.types import ProfileOutcome, RunStatus
from profiling.transport.schemas import ErrorResponse, ProfileCallbackRequest

log = logging.getLogger(__name__)

# 문서 1 §7.7 경로. tools/fake_backend/app.py의 CALLBACK_PATH와 같아야 한다 (테스트에서 대조).
CALLBACK_PATH = "/api/internal/v1/recipients/{recipientUserId}/profile"


# ---------------------------------------------------------------------------
# 내부 자료형 → 7.7 DTO (순수 함수, HTTP 없음)
# ---------------------------------------------------------------------------


def to_callback(outcome: ProfileOutcome) -> ProfileCallbackRequest:
    """ProfileOutcome → ProfileCallbackRequest. 태그는 넣지 않는다.

    RESULT_READY가 아니거나 검색 결과가 없으면 ValueError — 호출자(api.run_and_callback)가 이미 거르지만
    여기서도 막아서 잘못된 본문이 나가지 않게 한다. recommendedProductIds는 SearchResult.product_ids 순서 그대로(= 순위).
    """
    if outcome.status != RunStatus.RESULT_READY:
        raise ValueError(f"콜백은 RESULT_READY만 보낸다: status={outcome.status}")
    if outcome.search is None:
        raise ValueError("콜백에 보낼 검색 결과(search)가 없다")
    return ProfileCallbackRequest(
        recipientUserId=outcome.recipient_user_id,
        sourceVersion=outcome.source_version,
        recommendedProductIds=list(outcome.search.product_ids[:30]),
    )


def _status_from_response(status_code: int) -> RunStatus:
    """HTTP 상태 → 실행 기록의 다음 상태 (모듈 docstring 표)."""
    if status_code == 200:
        return RunStatus.DELIVERED
    if status_code == 409:
        return RunStatus.SUPERSEDED
    if 400 <= status_code < 500:
        return RunStatus.FAILED
    return RunStatus.RESULT_READY  # 5xx — 재시도 대상


def _error_code(res: httpx.Response) -> str:
    """오류 응답 본문의 error.code. 봉투가 아니면 본문 앞부분을 그대로."""
    try:
        return ErrorResponse.model_validate_json(res.content).error.code
    except (ValidationError, ValueError):
        return (res.text or "")[:80]


# ---------------------------------------------------------------------------
# HTTP 구현 (ports.BackendPort)
# ---------------------------------------------------------------------------


class HttpBackendPort:
    """ports.BackendPort 구현. httpx.Client 하나를 만들어 재사용한다(keep-alive, 스레드 안전). main.lifespan이 close()를 부른다.

    transport는 테스트 전용 — httpx.MockTransport를 넣으면 네트워크 없이 응답을 흉내 낼 수 있다. 운영에서는 넘기지 않는다.
    """

    def __init__(self, base_url: str, token: str, timeout_s: float, *, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout_s, transport=transport)

    # ---- ports.BackendPort --------------------------------------------------

    def send_profile_callback(self, outcome: ProfileOutcome) -> RunStatus:
        rid, sv = outcome.recipient_user_id, outcome.source_version
        try:
            body = to_callback(outcome)
        except ValueError as e:
            log.error("7.7 콜백 본문 생성 실패 recipient=%s source_version=%s: %s", rid, sv, e)
            return RunStatus.FAILED

        path = CALLBACK_PATH.format(recipientUserId=rid)
        t0 = time.perf_counter()
        try:
            res = self._client.post(path, content=body.model_dump_json())
        except httpx.RequestError as e:  # 타임아웃·연결 실패·DNS — 네트워크 층
            log.warning("7.7 콜백 recipient=%s source_version=%s → 네트워크 오류 %s: %s (재시도 대상)",
                        rid, sv, type(e).__name__, e)
            return RunStatus.RESULT_READY

        status = _status_from_response(res.status_code)
        elapsed = time.perf_counter() - t0
        n = len(body.recommendedProductIds)
        if status is RunStatus.DELIVERED:
            log.info("7.7 콜백 recipient=%s source_version=%s → %s DELIVERED ids=%d (%.2fs)", rid, sv, res.status_code, n, elapsed)
        elif status is RunStatus.SUPERSEDED:
            log.info("7.7 콜백 recipient=%s source_version=%s → 409 SUPERSEDED (더 새 버전 있음, 폐기) (%.2fs)", rid, sv, elapsed)
        elif status is RunStatus.FAILED:
            log.error("7.7 콜백 recipient=%s source_version=%s → %s FAILED code=%s (재시도 없음) (%.2fs)",
                      rid, sv, res.status_code, _error_code(res), elapsed)
        else:
            log.warning("7.7 콜백 recipient=%s source_version=%s → %s code=%s (재시도 대상) (%.2fs)",
                        rid, sv, res.status_code, _error_code(res), elapsed)
        return status

    # ---- 수명 ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()
