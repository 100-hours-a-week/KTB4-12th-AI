"""Free adapter tests: provider transport and ledger are fully synthetic."""

import json
from types import SimpleNamespace

import httpx
import pytest

from integration.profiler_boundary import (
    BudgetedExtractor,
    RequiredStore,
    ExecutionState,
)
from profiling.ports import TagExtractError


class Budget:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def reserve(self, body):
        self.events.append(("reserve", body))
        if self.fail:
            raise RuntimeError("synthetic failed acknowledgment")
        return body | {
            "max_tokens": 1200,
            "provider": {
                "max_price": {"prompt": 0.11, "completion": 0.55, "request": 0},
                "allow_fallbacks": False,
            },
        }, "synthetic-attempt"

    def finish(self, ident, cost):
        self.events.append(("finish", ident, cost))

    def close(self):
        pass


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "missing_usage",
        "invalid_cost",
        "timeout",
        "error",
        "redirect",
        "bad_json",
        "reservation_denied",
    ],
)
def test_reserve_before_wire_and_unknown_retention(mode):
    budget = Budget(fail=mode == "reservation_denied")
    wire = []

    def provider(request):
        assert budget.events[0][0] == "reserve"
        wire.append(json.loads(request.content))
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if mode == "error":
            return httpx.Response(429, text="PRIVATE_PROVIDER_PROMPT")
        if mode == "redirect":
            return httpx.Response(
                302, headers={"location": "https://unapproved.invalid/"}
            )
        if mode == "bad_json":
            return httpx.Response(200, text="PRIVATE_PROVIDER_PROMPT")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"tags":[]}'}, "finish_reason": "stop"}
                ],
                "usage": {}
                if mode == "missing_usage"
                else {"cost": "0.001" if mode == "invalid_cost" else 0.001},
            },
        )

    extractor = BudgetedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-not-a-key",
        "openai/gpt-6-luna",
        budget=budget,
        transport=httpx.MockTransport(provider),
    )
    try:
        if mode in ["success", "missing_usage", "invalid_cost"]:
            extractor.extract("SYNTHETIC", "SYNTHETIC", {})
        else:
            with pytest.raises(TagExtractError) as error:
                extractor.extract("SYNTHETIC", "SYNTHETIC", {})
            assert "PRIVATE_PROVIDER_PROMPT" not in str(error.value)
        assert len(wire) == (0 if mode == "reservation_denied" else 1)
        if wire:
            assert (
                wire[0]["max_tokens"] == 1200
                and wire[0]["reasoning"]["effort"] == "low"
                and "temperature" not in wire[0]
            )
            assert budget.events[-1] == (
                "finish",
                "synthetic-attempt",
                0.001 if mode == "success" else None,
            )
    finally:
        extractor.close()


@pytest.mark.parametrize("tag_status", ["fail:timeout", "bad_json", "off"])
def test_required_store_blocks_degraded_commit_before_callback(tag_status):
    saved = []
    state = ExecutionState()
    store = RequiredStore(SimpleNamespace(save=saved.append), state)
    store.save(SimpleNamespace(status="RUNNING"))
    with pytest.raises(RuntimeError):
        store.save(SimpleNamespace(status="RESULT_READY", tag_status=tag_status))
    assert len(saved) == 2 and str(saved[-1].status) == "FAILED"
    store.save(SimpleNamespace(status="FAILED"))
    assert len(saved) == 3


def test_required_embedding_failure_blocks_even_if_algorithm_falls_back():
    saved = []
    state = ExecutionState()
    store = RequiredStore(SimpleNamespace(save=saved.append), state)
    store.save(SimpleNamespace(status="RUNNING"))
    state.failure = "embedding_failed"
    with pytest.raises(RuntimeError):
        store.save(SimpleNamespace(status="RESULT_READY", tag_status="ok"))
    assert len(saved) == 2 and str(saved[-1].status) == "FAILED"


@pytest.mark.parametrize("missing", ["token", "tagger", "embedding", "key"])
def test_guarded_startup_rejects_required_configuration_before_database_or_wire(
    monkeypatch, missing
):
    import asyncio
    from pydantic import SecretStr
    from profiling.settings import Settings
    from integration import profiler_runtime

    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")

    values = {
        "SERVICE_TOKEN": "synthetic-token",
        "TAGGER": "openrouter",
        "CATALOG_SOURCE": "db",
        "EMBEDDING_DIR": "/synthetic",
        "OPENROUTER_API_KEY": SecretStr("synthetic-key"),
    }
    values[
        {
            "token": "SERVICE_TOKEN",
            "tagger": "TAGGER",
            "embedding": "EMBEDDING_DIR",
            "key": "OPENROUTER_API_KEY",
        }[missing]
    ] = {"token": "", "tagger": "none", "embedding": None, "key": None}[missing]
    settings = Settings(_env_file=None, **values)
    monkeypatch.setattr(profiler_runtime, "get_settings", lambda: settings)

    async def case():
        with pytest.raises(RuntimeError):
            async with profiler_runtime.lifespan(
                SimpleNamespace(state=SimpleNamespace())
            ):
                pass

    asyncio.run(case())


def test_new_llm_health_does_not_claim_budget_guard_from_enabled_alone():
    from integration.profiler_health import model_state

    health = {
        "llm": {"enabled": True, "model": "openai/gpt-6-luna", "state": "unknown"}
    }
    assert model_state(health)["required"] is False
    health["integration"] = {"modelRequired": True, "budgetGuard": "central-account"}
    assert model_state(health)["required"] is True
    assert model_state(health)["state"] == "unknown"


def test_embedding_failure_is_not_lost_when_algorithm_degrades():
    import numpy as np
    from integration.profiler_generation import GuardedEmbedding

    state = ExecutionState()
    saved = []
    store = RequiredStore(SimpleNamespace(save=saved.append), state)
    embedding = GuardedEmbedding(
        SimpleNamespace(encode=lambda texts: np.zeros((len(texts), 768))),
        SimpleNamespace(check=lambda: None),
        state,
    )
    store.save(SimpleNamespace(status="RUNNING"))
    with pytest.raises(RuntimeError):
        embedding.encode(["SYNTHETIC"])
    with pytest.raises(RuntimeError):
        store.save(SimpleNamespace(status="RESULT_READY", tag_status="ok"))
    assert str(saved[-1].status) == "FAILED" and saved[-1].search is None


def test_generation_failure_at_wire_boundary_prevents_reservation_and_send():
    budget = Budget()
    wire = []
    extractor = BudgetedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-key",
        "openai/gpt-6-luna",
        budget=budget,
        transport=httpx.MockTransport(lambda r: wire.append(r)),
    )

    def invalid_generation():
        raise RuntimeError("synthetic mismatch")

    extractor.bind_generation(invalid_generation)
    try:
        with pytest.raises(TagExtractError):
            extractor.extract("SYNTHETIC", "SYNTHETIC", {})
        assert not budget.events and not wire
    finally:
        extractor.close()


def test_forwarded_request_preserves_all_timeout_and_custom_extensions():
    budget = Budget()
    seen = []

    def provider(request):
        seen.append(dict(request.extensions))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"tags":[]}'}, "finish_reason": "stop"}
                ]
            },
        )

    extractor = BudgetedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-key",
        "openai/gpt-6-luna",
        budget=budget,
        timeout_s=0.123,
        transport=httpx.MockTransport(provider),
    )
    extractor._client.event_hooks["request"] = [
        lambda request: request.extensions.update(synthetic_marker="preserved")
    ]
    try:
        extractor.extract("SYNTHETIC", "SYNTHETIC", {})
        assert seen == [
            {
                "timeout": {
                    "connect": 0.123,
                    "read": 0.123,
                    "write": 0.123,
                    "pool": 0.123,
                },
                "synthetic_marker": "preserved",
            }
        ]
    finally:
        extractor.close()


@pytest.mark.parametrize("case", ["low_rating", "filtered_preference", "empty_input"])
def test_deliberate_no_model_result_keeps_profile_and_callback(case):
    from datetime import datetime, timezone
    from uuid import UUID
    from profiling import intake, pipeline
    from profiling.schemas import ProductRecord
    from profiling.types import ProfileRequest, Review, RunStatus, CallbackResult

    product = ProductRecord(
        productId=101,
        name="합성 상품",
        brand="SYNTHETIC",
        description=None,
        categoryId=11,
        categoryName="합성 분류",
        parentCategoryId=1,
        parentCategoryName="합성 대분류",
        price=1000,
        availability="available",
        updatedAt=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    catalog = SimpleNamespace(
        active=lambda: (UUID("00000000-0000-0000-0000-000000000001"), [product]),
        by_id=lambda ident: product if ident == 101 else None,
    )
    preference = "ㅋㅋㅋㅋㅋㅋ" if case == "filtered_preference" else None
    reviews = (
        [Review(product_id=101, rating=2, review_text=None)]  # Rating alone is not an explicit disliked tag.
        if case == "low_rating"
        else []
    )
    request = ProfileRequest(
        recipient_user_id=42,
        source_version=1,
        gift_preference=preference,
        disliked_categories=[],
        reviews=reviews,
    )

    class Store:
        def __init__(self):
            self.saved = []

        def save(self, outcome):
            self.saved.append(outcome.model_copy(deep=True))

    class Recipient:
        def __init__(self):
            self.profile = None

        def get(self, rid):
            return self.profile

        def upsert(self, profile):
            self.profile = profile

    class Tagger:
        model = "openai/gpt-6-luna"
        calls = 0

        def extract(self, *args):
            self.calls += 1
            raise AssertionError("Deliberate no-signal path must not call model")

    class Backend:
        def __init__(self):
            self.callbacks = []

        def send_profile_callback(self, outcome):
            self.callbacks.append(outcome)
            return CallbackResult(status=RunStatus.DELIVERED, http_status=200)

    baseline = Store()
    baseline_recipient = Recipient()
    baseline_tagger = Tagger()
    original = pipeline.profile(
        request,
        catalog=catalog,
        store=baseline,
        recipient_store=baseline_recipient,
        pool_size=30,
        tagger=baseline_tagger,
    )
    raw = Store()
    recipient = Recipient()
    tagger = Tagger()
    backend = Backend()
    guarded = RequiredStore(raw, ExecutionState())
    intake.run_and_callback(
        request, catalog, guarded, backend, 30, recipient, max_attempts=1, tagger=tagger
    )
    expected = "skip" if case == "empty_input" else "skip_no_signal"
    assert original.status == RunStatus.RESULT_READY and original.tag_status == expected
    assert baseline_tagger.calls == tagger.calls == 0
    assert (
        raw.saved[-1].status == RunStatus.DELIVERED
        and raw.saved[-1].tag_status == expected
    )
    assert recipient.profile is not None and baseline_recipient.profile is not None
    assert (
        recipient.profile.recommended_product_ids
        == baseline_recipient.profile.recommended_product_ids
    )
    assert (
        recipient.profile.category_scores == baseline_recipient.profile.category_scores
    )
    assert (
        len(backend.callbacks) == 1
        and backend.callbacks[0].search.product_ids == original.search.product_ids
    )


def test_optional_pgvector_similarity_failure_is_guarded_and_redacted():
    import numpy as np
    from integration.profiler_generation import GuardedEmbedding

    state = ExecutionState()
    def fail(*args):
        raise RuntimeError('synthetic private query')
    guarded = GuardedEmbedding(SimpleNamespace(similarities=fail), SimpleNamespace(check=lambda: None), state)
    with pytest.raises(RuntimeError, match='^Required embedding generation/execution failed$'):
        guarded.similarities([1], np.eye(768)[:1])
    assert state.failure == 'embedding_failed'


def test_guarded_similarity_fallback_preserves_shared_vector_dot_product():
    import numpy as np
    from integration.profiler_generation import GuardedEmbedding

    vectors = np.eye(768)[:2]
    state = ExecutionState()
    guarded = GuardedEmbedding(SimpleNamespace(product_vectors=lambda ids: vectors), SimpleNamespace(check=lambda: None), state)
    np.testing.assert_array_equal(guarded.similarities([1, 2], vectors), vectors @ vectors.T)
