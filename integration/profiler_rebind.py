"""Drain accepted Emmet work before swapping one validated, retained Search binding."""

import asyncio
import logging
import hmac
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Annotated

import anyio
from fastapi import Depends, Header, HTTPException, Request
from profiling.intake import settings_dep
from profiling.ports import NoActiveCatalog
from profiling.search_api import SearchCatalog
from profiling.settings import Settings
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)
REFRESH_PATH = "/api/internal/v1/ai/profile/catalog/refresh"


class RefreshFailure(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


class ManagedSearch:
    """Stable ports plus a Supervisor gate; upstream jobs keep their original ports."""

    def __init__(self, catalog, supervisor, factory, *, unfinished_runs=None):
        self.current = catalog
        self.supervisor = supervisor
        self.factory = factory
        self.unfinished_runs = unfinished_runs
        self.condition = threading.Condition()
        self.refresh_lock = threading.Lock()
        self.local = threading.local()
        self.pending = self.running = self.rejected = 0
        self.cleanup_failures = 0
        self.paused = self.closing = self.closed = False
        self.readers = Counter()
        self.retired = set()

    def _selected(self):
        return getattr(self.local, "catalog", self.current)

    @contextmanager
    def _borrow(self, *, admission=False):
        with self.condition:
            if (self.closed or admission and self.paused) and not hasattr(
                self.local, "catalog"
            ):
                raise NoActiveCatalog("Profiler catalog admission is paused")
            catalog = self._selected()
            self.readers[catalog] += 1
        try:
            yield catalog
        finally:
            with self.condition:
                self.readers[catalog] -= 1
                close = self._retire_ready(catalog)
            if close:
                self._close(catalog)

    def _close(self, catalog):
        try:
            catalog.close()
        except Exception:  # noqa: BLE001 — cleanup must not leave admission paused.
            with self.condition:
                self.cleanup_failures += 1
            logger.warning(
                "Retired Search client cleanup failed; its server lease expires"
            )

    def _retire_ready(self, catalog):
        if catalog in self.retired and not self.readers[catalog]:
            self.retired.remove(catalog)
            self.readers.pop(catalog, None)
            return True
        return False

    def check(self):
        with self._borrow() as catalog:
            return catalog.check()

    def active(self):
        with self._borrow(admission=True) as catalog:
            return catalog.active()

    def by_id(self, ident):
        with self._borrow() as catalog:
            return catalog.by_id(ident)

    def validate_disliked_categories(self, categories):
        with self._borrow() as catalog:
            return catalog.validate_disliked_categories(categories)

    def product_vectors(self, ids):
        with self._borrow() as catalog:
            return catalog.product_vectors(ids)

    def encode(self, texts):
        with self._borrow() as catalog:
            return catalog.encode(texts)

    @property
    def catalog_version(self):
        with self.condition:
            return self._selected().catalog_version

    def tag_taxonomy(self, profile):
        # Analysis runs on a retained thread binding; never stamp old analyses
        # with the taxonomy of a later generation.
        with self._borrow() as catalog:
            contract = catalog.contract
            if str(profile.catalog_version_id) != contract['catalogVersion']:
                raise ValueError('Analysis taxonomy generation mismatch')
            return {'productTagsRule': contract['productTagsRule'],
                    'productTagsSha256': contract['productTagsSha256'],
                    'validatorVersion': profile.validator_version}

    @property
    def version(self):
        with self.condition:
            return self._selected().version

    @property
    def total(self):
        with self.condition:
            return self._selected().total

    @property
    def matched(self):
        with self.condition:
            return self._selected().matched

    def submit(self, fn, *args, **kwargs):
        with self.condition:
            if self.paused or self.closing:
                self.rejected += 1
                return False
            self.pending += 1
            accepted = self.supervisor.submit(self._run, self.current, fn, args, kwargs)
            if not accepted:
                self.pending -= 1
            return accepted

    def _run(self, catalog, fn, args, kwargs):
        with self.condition:
            self.running += 1
        self.local.catalog = catalog
        try:
            return fn(*args, **kwargs)
        finally:
            del self.local.catalog
            with self.condition:
                self.running -= 1
                self.pending -= 1
                if self.closed and not self.running:
                    self.retired.add(self.current)
                close = self._retire_ready(catalog)
                self.condition.notify_all()
            if close:
                self._close(catalog)

    def stats(self):
        stats = self.supervisor.stats()
        with self.condition:
            return {
                **stats,
                "rejected": stats["rejected"] + self.rejected,
                "catalog_paused": self.paused,
                "catalog_pending": self.pending,
            }

    def status(self):
        with self.condition:
            return {
                "snapshotId": self.current.snapshot,
                "catalogVersionBound": str(self.current.catalog_version),
                "admissionPaused": self.paused,
                "pending": self.pending,
                "cleanupFailures": self.cleanup_failures,
            }

    def health_snapshot(self):
        """Pin one current binding for catalog, embedding and admission metadata."""
        with self.condition:
            current = self.current
            result = {
                "embedding": {
                    "enabled": True,
                    "version": current.version,
                    "matched": current.matched,
                    "total": current.total,
                },
                "integration": self.status(),
            }
            if self.paused or self.closed:
                result["catalog"] = {
                    "active": False,
                    "reason": "Profiler catalog admission is paused",
                }
                return result
            self.readers[current] += 1
        try:
            version, products = current.active()
            result["catalog"] = {
                "active": True,
                "version": str(version),
                "products": len(products),
            }
        except NoActiveCatalog as exc:
            result["catalog"] = {"active": False, "reason": str(exc)}
        finally:
            with self.condition:
                self.readers[current] -= 1
                close = self._retire_ready(current)
            if close:
                self._close(current)
        return result

    def refresh(self, expected_snapshot, timeout):
        if not self.refresh_lock.acquire(blocking=False):
            raise RefreshFailure("CATALOG_REFRESH_BUSY", 409)
        candidate = None
        try:
            with self.condition:
                if self.closing:
                    raise RefreshFailure("PROFILER_CLOSING")
                self.paused = True
                self.condition.notify_all()
                deadline = time.monotonic() + timeout
                while self.pending:
                    if self.closing:
                        raise RefreshFailure("PROFILER_CLOSING")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RefreshFailure("CATALOG_DRAIN_TIMEOUT", 409)
                    self.condition.wait(remaining)
                if self.closing:
                    raise RefreshFailure("PROFILER_CLOSING")
            if self.current.snapshot == expected_snapshot:
                try:
                    self.check()
                except NoActiveCatalog:
                    # Search can restart with the same snapshot but lose its leases.
                    pass
                else:
                    return {**self.status(), "admissionPaused": False, "changed": False}
            # An empty in-process queue does not prove durable work is finished.
            # Keep the old binding for failures/retries and unsent callbacks.
            changing_generation = self.current.snapshot != expected_snapshot
            if changing_generation and self.unfinished_runs is not None and self.unfinished_runs():
                raise RefreshFailure("CATALOG_DURABLE_WORK_REMAINS", 409)
            candidate = self.factory()
            if candidate.snapshot != expected_snapshot:
                raise RefreshFailure("TARGET_SNAPSHOT_MISMATCH", 409)
            if not changing_generation and getattr(candidate, 'contract', None) != getattr(self.current, 'contract', None):
                raise RefreshFailure("TARGET_CONTRACT_MISMATCH", 409)
            candidate.check()
            with self.condition:
                if self.closing:
                    raise RefreshFailure("PROFILER_CLOSING")
                old, self.current = self.current, candidate
                candidate = None
                self.retired.add(old)
                close = self._retire_ready(old)
            if close:
                self._close(old)
            return {**self.status(), "admissionPaused": False, "changed": True}
        except RefreshFailure:
            raise
        except Exception:  # noqa: BLE001 — redact failed candidate initialization.
            raise RefreshFailure("CATALOG_REFRESH_FAILED") from None
        finally:
            if candidate is not None:
                self._close(candidate)
            with self.condition:
                self.paused = self.closing
                self.condition.notify_all()
            self.refresh_lock.release()

    def stop(self, timeout_s=5.0):
        with self.condition:
            self.closing = self.paused = True
        dropped = self.supervisor.stop(timeout_s)
        with self.condition:
            self.pending = self.running
            self.condition.notify_all()
        return dropped

    def close(self):
        with self.condition:
            self.closed = self.closing = self.paused = True
            if not self.running:
                self.retired.add(self.current)
            close = self._retire_ready(self.current)
        if close:
            self._close(self.current)


def load_retained_catalog(settings):
    catalog = SearchCatalog(
        settings.SEARCH_BASE_URL,
        settings.SEARCH_TOKEN.get_secret_value()
        if settings.SEARCH_TOKEN
        else settings.SERVICE_TOKEN,
        retain_generation=True,
    )
    try:
        # Validate every row in bounded batches before admitting any new work.
        ids = [product.productId for product in catalog.products]
        for offset in range(0, len(ids), 500):
            catalog.product_vectors(ids[offset : offset + 500])
        catalog.encode(["합성 임베딩 준비 검증"])
        catalog.check()
        return catalog
    except Exception:
        catalog.close()
        raise


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedSnapshotId: str = Field(min_length=1, max_length=80)
    drainTimeoutSeconds: int = Field(default=30, ge=1, le=300, strict=True)


async def refresh_catalog(body: RefreshRequest, request: Request):
    manager = getattr(request.app.state, "shared_catalog", None)
    if manager is None:
        raise HTTPException(503, "SHARED_CATALOG_NOT_CONFIGURED")
    try:
        result = await anyio.to_thread.run_sync(
            manager.refresh,
            body.expectedSnapshotId,
            body.drainTimeoutSeconds,
        )
    except RefreshFailure as exc:
        raise HTTPException(exc.status, exc.code) from None
    return {**result, "admissionPaused": manager.status()["admissionPaused"]}


async def require_refresh_token(
    settings: Annotated[Settings, Depends(settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
):
    token = getattr(settings, "SEARCH_TOKEN", "") or settings.SERVICE_TOKEN
    if hasattr(token, "get_secret_value"):
        token = token.get_secret_value()
    if not token:
        raise HTTPException(503, "REFRESH_AUTH_NOT_CONFIGURED")
    if not authorization or not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "UNAUTHORIZED")


def unfinished_profile_runs(engine):
    from sqlalchemy import text
    with engine.connect() as connection:
        return connection.execute(text("""SELECT count(*) FROM ai_profile.profile_runs
            WHERE status NOT IN ('DELIVERED','SUPERSEDED')""")).scalar_one()


def install(app, settings):
    plain_catalog = app.state.catalog

    def factory():
        return load_retained_catalog(settings)

    manager = ManagedSearch(factory(), app.state.supervisor, factory,
                            unfinished_runs=lambda: unfinished_profile_runs(app.state.engine))
    app.state.catalog = app.state.embedding = app.state.shared_catalog = manager
    app.state.supervisor = manager
    plain_catalog.close()
    from integration.profile_callback import VisibleProductsBackend
    app.state.backend = VisibleProductsBackend(app.state.backend, settings.SEARCH_BASE_URL,
        settings.SEARCH_TOKEN.get_secret_value() if settings.SEARCH_TOKEN else settings.SERVICE_TOKEN)
    if not any(getattr(route, "path", None) == REFRESH_PATH for route in app.routes):
        app.add_api_route(
            REFRESH_PATH,
            refresh_catalog,
            methods=["POST"],
            dependencies=[Depends(require_refresh_token)],
        )
    return manager


async def keep_binding_alive(manager):
    while True:
        await asyncio.sleep(30)
        try:
            await anyio.to_thread.run_sync(manager.check)
        except Exception:  # noqa: BLE001 — heartbeat failure is redacted; request checks stay mandatory.
            logger.warning(
                "Shared catalog lease could not be renewed; binding checks remain required"
            )
