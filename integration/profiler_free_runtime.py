"""Explicit zero-provider fixture entrypoint; same generation/Node guards, model disabled."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from integration.shared_search_binding import SharedSearchBinding
from profiling import main
from profiling.settings import get_settings
from integration.profiler_boundary import RequiredStore, ExecutionState
from integration.profiler_generation import (
    BoundGeneration,
    GuardedCatalog,
    GuardedEmbedding,
)


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    if settings.TAGGER != "none" or (
        settings.OPENROUTER_API_KEY
        and settings.OPENROUTER_API_KEY.get_secret_value().strip()
    ):
        raise RuntimeError(
            "Free fixture requires model disabled and no provider credential"
        )
    if main.strict_problems(settings) or (settings.CATALOG_SOURCE != "search_http" and not settings.EMBEDDING_DIR):
        raise RuntimeError(
            "Free fixture requires explicit auth/DB/catalog/encoder configuration"
        )
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
        if app.state.tagger is not None:
            raise RuntimeError("Free fixture must have no model adapter")
        binding = (SharedSearchBinding(app.state.catalog) if settings.CATALOG_SOURCE == "search_http"
                   else BoundGeneration(Path(settings.EMBEDDING_DIR), app.state.engine))
        if (
            app.state.embedding is None
            or app.state.embedding.matched != binding.count
            or app.state.embedding.total != binding.count
        ):
            raise RuntimeError("Free fixture requires all vector rows")
        execution = ExecutionState()
        app.state.catalog = GuardedCatalog(app.state.catalog, binding)
        app.state.embedding = GuardedEmbedding(app.state.embedding, binding, execution)
        app.state.store = RequiredStore(
            app.state.store, execution, binding, required_model=False
        )
        app.state.embedding.encode(["합성 임베딩 준비 검증"])
        if settings.CATALOG_SOURCE != "search_http":
            binding.execution_probe = lambda: (
                app.state.embedding.embedding._encoder._proc is not None
                and app.state.embedding.embedding._encoder._proc.poll() is None
            )
        app.state.integration = {
            "modelRequired": False,
            "budgetGuard": "not-applicable-free",
            "catalogVersionBound": binding.version,
            "synthetic": True,
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


app = main.app
app.router.lifespan_context = lifespan
_original_health = main._health_sync


def health(state):
    result = _original_health(state)
    result["integration"] = {
        **getattr(state, "integration", {"synthetic": True}),
        **result.get("integration", {}),
    }
    return result


main._health_sync = health
