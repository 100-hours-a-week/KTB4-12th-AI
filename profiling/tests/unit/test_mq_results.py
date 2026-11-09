"""mq.results — 결과 메시지 바이트(설계 §3 · BE-01 ~ 03)."""
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from profiling.mq import results as r

RID = "8a3851f6-1a29-4f44-a812-f6d05ad3d8e1"
NOW = datetime(2026, 11, 8, 3, 4, 5, 678901, tzinfo=UTC)


def test_성공_모양():
    doc = json.loads(r.success_body(RID, 42, 7, [5, 3, 9], now=NOW))
    assert doc == {"schemaVersion": 1, "requestId": RID, "createdAt": "2026-11-08T03:04:05.678Z",
                   "payload": {"recipientUserId": 42, "sourceVersion": 7, "outcome": "SUCCEEDED",
                               "data": {"recommendedProductIds": [5, 3, 9]}}}


def test_빈_추천도_성공():
    doc = json.loads(r.success_body(RID, 42, 7, [], now=NOW))
    assert doc["payload"]["outcome"] == "SUCCEEDED" and doc["payload"]["data"]["recommendedProductIds"] == []
    assert "error" not in doc["payload"]


@pytest.mark.parametrize("ids", [[1, 1], [0], [-3], [True], ["1"], list(range(1, 32))])
def test_추천_번호_규격_위반은_거부(ids):
    with pytest.raises(ValueError):
        r.success_body(RID, 42, 7, ids, now=NOW)


def test_30개는_허용():
    assert len(json.loads(r.success_body(RID, 1, 1, list(range(1, 31)), now=NOW))["payload"]["data"]["recommendedProductIds"]) == 30


def test_실패_모양_과_안전한_메시지():
    doc = json.loads(r.failure_body(RID, 42, 7, "REQUEST_CONFLICT", now=NOW))
    assert doc["payload"]["outcome"] == "FAILED" and "data" not in doc["payload"]
    assert doc["payload"]["error"] == {"code": "REQUEST_CONFLICT", "message": r.FAILURE_MESSAGES["REQUEST_CONFLICT"]}


def test_모르는_실패_코드는_거부():
    with pytest.raises(ValueError):
        r.failure_body(RID, 42, 7, "STORE_FAILED", now=NOW)


def test_createdAt_은_UTC_Z():
    kst = NOW.astimezone(timezone(timedelta(hours=9)))
    assert json.loads(r.success_body(RID, 1, 1, [], now=kst))["createdAt"] == "2026-11-08T03:04:05.678Z"
