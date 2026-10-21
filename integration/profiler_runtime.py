"""Integrated runtime with provider key limits or an explicitly selected ledger."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from integration.shared_search_binding import SharedSearchBinding

from profiling import main, llm
from profiling.settings import get_settings
from integration.profiler_boundary import (
    ProviderLimitedExtractor,
    ExecutionState,
    RequiredStore,
)
from integration.profiler_generation import (
    BoundGeneration,
    GuardedCatalog,
    GuardedEmbedding,
)
from integration.provider_key import ProviderKey, KeyUnavailable
from integration.service_token import ServiceToken, shared_token_configured


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    credential_env = dict(os.environ)
    # Native Settings also reads .env. Keep that initial key without bypassing
    # an explicit file/AWS source or the managed record's saved marker.
    initial_key = getattr(settings, "OPENROUTER_API_KEY", None)
    if (initial_key is not None and "PROFILING_OPENROUTER_API_KEY" not in credential_env
            and not credential_env.get("PROFILING_OPENROUTER_API_KEY_FILE")
            and not credential_env.get("OPENROUTER_API_KEY_PROFILER")
            and not credential_env.get("OPENROUTER_API_KEY_PROFILER_FILE")
            and credential_env.get("AI_CREDENTIAL_BACKEND", "files") == "files"
            and credential_env.get("AI_CREDENTIAL_LAYOUT", "separate") == "separate"):
        credential_env["PROFILING_OPENROUTER_API_KEY"] = initial_key.get_secret_value()
    source = ProviderKey.from_environment("profiler", credential_env)
    shared_auth = shared_token_configured()
    token = ServiceToken.from_environment("profiler") if shared_auth else None
    app.state.provider_key = source
    app.state.be_token_provider = token.snapshot if token else None
    if (
        (not shared_auth and not settings.SERVICE_TOKEN.strip())
        or settings.TAGGER != "openrouter"
        or settings.CATALOG_SOURCE not in ("db", "search_http")
        or (settings.CATALOG_SOURCE != "search_http" and not settings.EMBEDDING_DIR)
    ):
        raise RuntimeError(
            "Integrated required authentication/model/embedding settings missing"
        )
    if main.strict_problems(settings, managed_auth=shared_auth):
        raise RuntimeError("Integrated strict startup settings invalid")
    def provider_factory(*args, **kwargs):
        return ProviderLimitedExtractor(*args, **kwargs, credential_source=source)
    original = llm.OpenRouterTagExtractor
    llm.OpenRouterTagExtractor = provider_factory
    context = main.lifespan(app)
    entered = False
    heartbeat = None
    try:
        await context.__aenter__()
        entered = True
        from integration.recipient_data import RecipientInputStore, RecordingRecipientStore
        app.state.recipient_data = RecipientInputStore(settings.DATABASE_URL)
        app.state.recipient_store = RecordingRecipientStore(app.state.recipient_store, app.state.recipient_data)
        if settings.CATALOG_SOURCE == "search_http":
            from integration.profiler_rebind import install, keep_binding_alive
            manager = await asyncio.to_thread(install, app, settings)
            app.state.recipient_store.taxonomy = manager.tag_taxonomy
            heartbeat = asyncio.create_task(keep_binding_alive(manager))
        binding = (SharedSearchBinding(app.state.catalog) if settings.CATALOG_SOURCE == "search_http"
                   else BoundGeneration(Path(settings.EMBEDDING_DIR), app.state.engine))
        if app.state.tagger is None or app.state.embedding is None:
            raise RuntimeError("Required model/embedding adapter missing")
        if (
            app.state.embedding.matched != binding.count
            or app.state.embedding.total != binding.count
        ):
            raise RuntimeError(
                "All active products must have exactly matching vector rows"
            )
        execution = ExecutionState()
        app.state.catalog = GuardedCatalog(app.state.catalog, binding)
        app.state.embedding = GuardedEmbedding(app.state.embedding, binding, execution)
        app.state.store = RequiredStore(app.state.store, execution, binding)
        # Local CPU warm-up (via Search in shared mode) precedes any model work.
        app.state.embedding.encode(["합성 임베딩 준비 검증"])
        app.state.tagger.bind_generation(binding.check)
        if settings.CATALOG_SOURCE != "search_http":
            binding.execution_probe = lambda: (
                app.state.embedding.embedding._encoder._proc is not None
                and app.state.embedding.embedding._encoder._proc.poll() is None
            )
        app.state.integration = {
            "modelRequired": True,
            "budgetGuard": "provider-key-limits",
            "catalogVersionBound": binding.version,
        }
        yield
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if entered:
            await context.__aexit__(None, None, None)
        else:
            for resource in ('catalog', 'embedding', 'backend', 'tagger'):
                close = getattr(getattr(app.state, resource, None), 'close', None)
                if close: close()
            engine = getattr(app.state, 'engine', None)
            if engine is not None: engine.dispose()
        llm.OpenRouterTagExtractor = original


app = main.app
app.router.lifespan_context = lifespan
_original_health = main._health_sync
_original_ready = main._ready_checks


@app.get("/internal/credentials")
async def credential_status(request: main.Request):
    from fastapi import HTTPException
    from integration.service_token import matches
    if not await asyncio.to_thread(matches, "profiler", request.headers.get("authorization", "")):
        raise HTTPException(401, "UNAUTHORIZED")
    source = request.app.state.provider_key
    key, token = await asyncio.gather(asyncio.to_thread(source.runtime_status),
                                     asyncio.to_thread(ServiceToken.from_environment("profiler").status))
    token["usage"] = {"state": "not-collected", "lastSuccessfulCallAt": None, "scope": "responding-process"}
    return {"service": "profiler", "environment": source.environment, "provider": key, "backend": token}


def integrated_health(state):
    result = _original_health(state)
    result["integration"] = {
        **getattr(state, "integration", {"modelRequired": True, "budgetGuard": "unverified"}),
        **result.get("integration", {}),
    }
    if getattr(state, "provider_key", None):
        result["integration"]["providerKeyConfigured"] = state.provider_key.status()["configured"]
    return result


def integrated_ready(state):
    checks = _original_ready(state)
    checks["integration_required"] = (
        getattr(state, "integration", {}).get("modelRequired") is True
    )
    checks["required_embedding"] = getattr(state, "embedding", None) is not None
    if getattr(state, "provider_key", None):
        checks["provider_key"] = state.provider_key.status()["configured"]
    if getattr(state, "be_token_provider", None):
        try:
            state.be_token_provider()
            checks["service_token"] = True
        except KeyUnavailable:
            checks["service_token"] = False
    return checks


main._health_sync = integrated_health
main._ready_checks = integrated_ready
