"""adapters/backend — to_callback 변환과 HttpBackendPort의 상태 코드 해석. httpx.MockTransport로 네트워크 없이."""

import json
from uuid import UUID

import httpx
import pytest

from profiling.adapters import backend_port_http as backend_mod
from profiling.adapters.backend_port_http import HttpBackendPort, to_callback
from profiling.profile.ports import BackendPort
from profiling.profile.types import ProfileOutcome, RunStatus, SearchResult
from tools.fake_backend import app as fake

CV = UUID(int=1)   # 시험용 카탈로그 버전 ID


def _outcome(status: RunStatus = RunStatus.RESULT_READY, ids: list[int] | None = None) -> ProfileOutcome:
    ids = [101, 102, 103] if ids is None else ids
    return ProfileOutcome(recipient_user_id=9073, source_version=3, status=status,
                          search=SearchResult(product_ids=ids, query_text="", catalog_version_id=CV))


# ---------------------------------------------------------------- to_callback


def test_to_callback_maps_fields_and_no_tags() -> None:
    body = to_callback(_outcome())
    assert body.recipientUserId == 9073 and body.sourceVersion == 3 and body.profileStatus == "COMPLETED"
    assert body.recommendedProductIds == [101, 102, 103]
    assert "preferredTags" not in body.model_dump() and "dislikedTags" not in body.model_dump()   # DR-035


def test_to_callback_rejects_non_ready_or_no_search() -> None:
    with pytest.raises(ValueError):
        to_callback(_outcome(status=RunStatus.FAILED))
    with pytest.raises(ValueError):
        to_callback(ProfileOutcome(recipient_user_id=1, source_version=0, status=RunStatus.RESULT_READY))


def test_callback_path_matches_fake_backend() -> None:
    assert backend_mod.CALLBACK_PATH == fake.CALLBACK_PATH


# ---------------------------------------------------------------- HttpBackendPort


def _backend(handler) -> HttpBackendPort:
    return HttpBackendPort("http://backend.test", "tok", 1.0, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status_code,expected", [
    (200, RunStatus.DELIVERED),
    (409, RunStatus.SUPERSEDED),
    (400, RunStatus.FAILED),
    (401, RunStatus.FAILED),
    (500, RunStatus.RESULT_READY),
    (503, RunStatus.RESULT_READY),
])
def test_status_mapping(status_code: int, expected: RunStatus) -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization"); seen["body"] = json.loads(req.content)
        body = {"message": "x", "error": {"code": "STALE_SOURCE_VERSION" if status_code == 409 else "E", "traceId": None}}
        return httpx.Response(status_code, json=body if status_code != 200 else {"message": "ok", "data": {}})

    b = _backend(handler)
    assert isinstance(b, BackendPort)
    assert b.send_profile_callback(_outcome()) is expected
    assert seen["url"] == "http://backend.test/api/internal/v1/recipients/9073/profile"   # 경로에 수신자 ID
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["recipientUserId"] == 9073 and seen["body"]["recommendedProductIds"] == [101, 102, 103]
    b.close()


def test_network_error_is_retryable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)

    assert _backend(handler).send_profile_callback(_outcome()) is RunStatus.RESULT_READY


def test_bad_outcome_is_failed_without_http() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP가 나가면 안 된다")

    assert _backend(handler).send_profile_callback(_outcome(status=RunStatus.FAILED)) is RunStatus.FAILED


def test_ids_30_pass_through_in_order() -> None:
    ids = list(range(1, 31))                      # 31개 이상은 SearchResult(max_length=30)가 먼저 막는다
    assert to_callback(_outcome(ids=ids)).recommendedProductIds == ids
