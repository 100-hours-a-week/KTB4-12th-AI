"""끝에서 끝 — TestClient로 앱을 띄워 7.6 → 202 → (백그라운드) profile → 7.7 콜백까지. HTTP 콜백은 가짜 BackendPort가 받는다."""

from fastapi.testclient import TestClient

from profiling.main import app
from profiling.profile.types import CallbackResult, RunStatus


class RecordingBackend:
    def __init__(self):
        self.sent = []

    def send_profile_callback(self, outcome):
        self.sent.append(outcome)
        return CallbackResult(status=RunStatus.DELIVERED, http_status=200)


def test_v1_request_end_to_end() -> None:
    with TestClient(app) as client:                     # lifespan: FileCatalogReader(예시 111건) · MemoryProfileRunStore(conftest: STORE=memory) · HttpBackendPort
        fake = RecordingBackend()
        app.state.backend = fake                        # 진짜 HTTP 대신 기록만 (ports 모양이면 교체 가능)
        body = {"recipientUserId": 9073, "sourceVersion": 3,
                "dislikedCategories": [{"categoryId": 802, "categoryName": "출산·육아용품"}],
                "giftPreference": None, "reviews": []}
        res = client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body)
        assert res.status_code == 202
        assert res.json()["data"] == {"recipientUserId": 9073, "sourceVersion": 3, "profileStatus": "PENDING"}

        # TestClient는 응답 뒤 BackgroundTasks를 동기로 실행한다 → 여기서 이미 끝나 있음
        assert len(fake.sent) == 1
        outcome = fake.sent[0]
        assert outcome.status is RunStatus.RESULT_READY
        assert len(outcome.search.product_ids) == 30
        assert outcome.validation.disliked_tags == ["출산·육아용품"] and outcome.input_hash
        saved = app.state.store.get(9073)                  # 콜백 뒤 run_and_callback이 결과를 실행 기록에 저장
        assert saved.status is RunStatus.DELIVERED and saved.callback_attempts == 1 and saved.search == outcome.search
        _, products = app.state.catalog.active()
        by_id = {p.productId: p for p in products}
        assert all(by_id[i].categoryName != "출산·육아용품" for i in outcome.search.product_ids)

        bad = client.post("/api/internal/v1/ai/profile/extract-and-pool", json={**body, "reviews": [{"productId": 1, "rating": 9}]})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_REQUEST"
        assert len(fake.sent) == 1                      # 400은 백그라운드로 가지 않음


def test_unknown_fields_are_accepted_and_logged(caplog) -> None:
    """계약에 없는 필드 → 400이 아니라 202 + CONTRACT_7_6_UNKNOWN_FIELD 경고 (Backend가 필드를 추가해도 연동 유지)."""
    with TestClient(app) as client:
        app.state.backend = RecordingBackend()
        body = {"recipientUserId": 9074, "sourceVersion": 1, "dislikedCategories": [{"categoryId": 802, "categoryName": "출산·육아용품", "weight": 1}],
                "giftPreference": None, "reviews": [], "extraTop": "x"}
        with caplog.at_level("WARNING", logger="profiling.transport.profile_intake"):
            res = client.post("/api/internal/v1/ai/profile/extract-and-pool", json=body)
        assert res.status_code == 202
        assert any("CONTRACT_7_6_UNKNOWN_FIELD" in r.message and "extraTop" in r.message and "weight" in r.message for r in caplog.records)
