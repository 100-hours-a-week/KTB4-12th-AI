"""결과 메시지 바이트 만들기 (설계 §3 · BE-01 ~ 03 사용자 확정).

  성공 {"schemaVersion":1, "requestId", "createdAt", "payload":{"recipientUserId", "sourceVersion", "outcome":"SUCCEEDED",
        "data":{"recommendedProductIds":[…]}}}
  실패 {"schemaVersion":1, "requestId", "createdAt", "payload":{"recipientUserId", "sourceVersion", "outcome":"FAILED",
        "error":{"code", "message"}}}

- createdAt = 결과 메시지를 처음 만든 UTC 시각(BE-02). 바이트는 한 번 만들어 저장하고 재발행 때 그대로 쓴다(store.set_result).
- recommendedProductIds: 중복 없는 양의 정수 0 ~ 30개, 배열 순서 = 추천 순위(BE-01). 빈 추천도 성공.
- 성공엔 data 만, 실패엔 error 만. HTTP 응답 봉투는 붙이지 않는다. 오류 message 에 원문 · 스택 · 외부 응답 · 비밀값 금지.

⚠️ 노션 안에서 결과 모양이 두 가지다(상세 설계 outcome · data / 구현 기준 절 profileStatus). 상세 설계(11-07, BE-01 ~ 03 확정)를
   따르고, 팀원 확인 뒤 바뀌면 이 파일만 고친다.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

SCHEMA_VERSION = 1
MAX_IDS = 30

# 실패 코드(설계 C · 실패 코드 초안)와 안전한 한 줄 메시지
FAILURE_MESSAGES = {
    "INVALID_REQUEST": "프로파일링 요청 데이터가 올바르지 않습니다.",
    "UNSUPPORTED_SCHEMA_VERSION": "지원하지 않는 프로파일링 메시지 버전입니다.",
    "REQUEST_CONFLICT": "기존 프로파일링 요청과 입력 데이터가 일치하지 않습니다.",
    "ANALYSIS_FAILED": "프로파일링 분석에 실패했습니다.",
    "ANALYSIS_RETRY_EXHAUSTED": "프로파일링 분석 재시도 한도를 초과했습니다.",
}


def _created_at(now: datetime | None) -> str:
    t = (now or datetime.now(UTC)).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _dump(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def success_body(request_id: str, recipient_user_id: int, source_version: int, product_ids: list[int], *,
                 now: datetime | None = None) -> bytes:
    ids = list(product_ids)
    if len(ids) > MAX_IDS or len(set(ids)) != len(ids) or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
        raise ValueError("recommendedProductIds 는 중복 없는 양의 정수 0 ~ 30개여야 한다")
    return _dump({"schemaVersion": SCHEMA_VERSION, "requestId": request_id, "createdAt": _created_at(now),
                  "payload": {"recipientUserId": recipient_user_id, "sourceVersion": source_version, "outcome": "SUCCEEDED",
                              "data": {"recommendedProductIds": ids}}})


def failure_body(request_id: str, recipient_user_id: int, source_version: int, code: str, *, now: datetime | None = None) -> bytes:
    if code not in FAILURE_MESSAGES:
        raise ValueError(f"모르는 실패 코드: {code}")
    return _dump({"schemaVersion": SCHEMA_VERSION, "requestId": request_id, "createdAt": _created_at(now),
                  "payload": {"recipientUserId": recipient_user_id, "sourceVersion": source_version, "outcome": "FAILED",
                              "error": {"code": code, "message": FAILURE_MESSAGES[code]}}})
