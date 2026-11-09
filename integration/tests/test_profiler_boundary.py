"""Free adapter tests: provider transport and ledger are fully synthetic."""

import json
from types import SimpleNamespace

import httpx
import pytest

from integration.profiler_boundary import (
    ProviderLimitedExtractor,
    RequiredStore,
    ExecutionState,
)
from profiling.ports import ResultRefused, TagExtractError






@pytest.mark.parametrize("tag_status", ["fail:timeout", "bad_json", "off"])
def test_tag_fallback_result_is_accepted(tag_status):
    """설계 CO-01 · 11-08 결정 A1: 태그 없는 추천(폴백)은 SUCCEEDED — 결과를 거절하지 않는다."""
    saved = []
    state = ExecutionState()
    store = RequiredStore(SimpleNamespace(save=saved.append), state)
    store.save(SimpleNamespace(status="RUNNING"))
    store.save(SimpleNamespace(status="RESULT_READY", tag_status=tag_status))
    assert len(saved) == 2 and str(saved[-1].status) == "RESULT_READY"


def test_embedding_failure_fallback_result_is_accepted():
    saved = []
    state = ExecutionState()
    store = RequiredStore(SimpleNamespace(save=saved.append), state)
    store.save(SimpleNamespace(status="RUNNING"))
    state.failure = "embedding_failed"
    store.save(SimpleNamespace(status="RESULT_READY", tag_status="ok"))
    assert len(saved) == 2 and str(saved[-1].status) == "RESULT_READY"


def test_generation_change_refuses_result_before_commit():
    """분석 도중 카탈로그 세대가 바뀌면 옛 카탈로그 순위 — 거절(DB 장애와 구분되는 ResultRefused)하고 FAILED 를 먼저 남긴다."""
    saved = []
    state = ExecutionState()

    def changed():
        raise RuntimeError("generation changed")

    store = RequiredStore(SimpleNamespace(save=saved.append), state, SimpleNamespace(check=changed))
    store.save(SimpleNamespace(status="RUNNING"))
    with pytest.raises(ResultRefused):
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
    assert state.failure == "embedding_failed"          # 실패 표시는 남고(로그) 결과는 폴백으로 받는다(A1)
    store.save(SimpleNamespace(status="RESULT_READY", tag_status="ok"))
    assert str(saved[-1].status) == "RESULT_READY"


def test_generation_failure_at_wire_boundary_prevents_reservation_and_send():
    wire = []
    extractor = ProviderLimitedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-key",
        "openai/gpt-6-luna",
        transport=httpx.MockTransport(lambda r: wire.append(r)),
    )

    def invalid_generation():
        raise RuntimeError("synthetic mismatch")

    extractor.bind_generation(invalid_generation)
    try:
        with pytest.raises(TagExtractError):
            extractor.extract("SYNTHETIC", "SYNTHETIC", {})
        assert not wire
    finally:
        extractor.close()


def test_forwarded_request_preserves_all_timeout_and_custom_extensions():
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

    extractor = ProviderLimitedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-key",
        "openai/gpt-6-luna",
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
def test_deliberate_no_model_result_keeps_profile_and_result(case):
    from datetime import datetime, timezone
    from uuid import UUID
    from profiling import pipeline, stores
    from profiling.schemas import ProductRecord
    from profiling.types import ProfileRequest, Review, RunStatus

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
    guarded = RequiredStore(raw, ExecutionState())
    # MQ processor path (profiling.mq.runtime.build.analyze): same pipeline call through the guarded store.
    outcome = pipeline.profile(
        request, catalog=catalog, store=guarded, recipient_store=recipient, pool_size=30, tagger=tagger
    )
    expected = "skip" if case == "empty_input" else "skip_no_signal"
    assert original.status == RunStatus.RESULT_READY and original.tag_status == expected
    assert baseline_tagger.calls == tagger.calls == 0
    assert (
        raw.saved[-1].status == RunStatus.RESULT_READY
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
    assert stores.result_payload(outcome) == stores.result_payload(original)


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
