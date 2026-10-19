"""Opt-in guarded runtime for the recorded Emmet revision and shared catalog adapter."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from integration.shared_search_binding import SharedSearchBinding

from profiling import main, llm
from profiling.settings import get_settings
from integration.profiler_boundary import (
    BudgetedExtractor,
    ProviderLimitedExtractor,
    REQUIRED_GUARDS,
    ExecutionState,
    RequiredStore,
)
from integration.profiler_generation import (
    BoundGeneration,
    GuardedCatalog,
    GuardedEmbedding,
)
from integration.shared_budget import SharedBudget


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    if (
        not settings.SERVICE_TOKEN.strip()
        or settings.TAGGER != "openrouter"
        or settings.CATALOG_SOURCE not in ("db", "search_http")
        or (settings.CATALOG_SOURCE != "search_http" and not settings.EMBEDDING_DIR)
        or settings.OPENROUTER_API_KEY is None
        or not settings.OPENROUTER_API_KEY.get_secret_value().strip()
    ):
        raise RuntimeError(
            "Integrated required authentication/model/embedding settings missing"
        )
    if main.strict_problems(settings):
        raise RuntimeError("Integrated strict startup settings invalid")
    mode = os.environ.get("AI_BUDGET_MODE", "").strip() or "guarded"
    budget = None
    if mode == "guarded":
        budget = SharedBudget(
            os.environ.get("PROFILING_BUDGET_URL", ""), os.environ.get("STORE_TOKEN", "")
        )
        budget.call("budget_summary")
        guard = "central-account"

        def guarded_factory(*args, **kwargs):
            return BudgetedExtractor(*args, **kwargs, budget=budget)
    elif mode == "provider-key-limits":
        guard = "provider-key-limits"

        def guarded_factory(*args, **kwargs):
            return ProviderLimitedExtractor(*args, **kwargs)
    else:
        raise RuntimeError("Unsupported AI_BUDGET_MODE")
    original = llm.OpenRouterTagExtractor
    llm.OpenRouterTagExtractor = guarded_factory
    context = main.lifespan(app)
    entered = False
    heartbeat = None
    try:
        await context.__aenter__()
        entered = True
        if settings.CATALOG_SOURCE == "search_http":
            from integration.profiler_rebind import install, keep_binding_alive
            manager = await asyncio.to_thread(install, app, settings)
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
            "budgetGuard": guard,
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
            if budget is not None:
                budget.close()
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


def integrated_health(state):
    result = _original_health(state)
    result["integration"] = {
        **getattr(state, "integration", {"modelRequired": True, "budgetGuard": "unverified"}),
        **result.get("integration", {}),
    }
    return result


def integrated_ready(state):
    checks = _original_ready(state)
    checks["integration_required"] = (
        getattr(state, "integration", {}).get("budgetGuard") in REQUIRED_GUARDS
    )
    checks["required_embedding"] = getattr(state, "embedding", None) is not None
    return checks


main._health_sync = integrated_health
main._ready_checks = integrated_ready
