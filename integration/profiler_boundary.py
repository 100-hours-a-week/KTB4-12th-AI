"""Cost and mandatory-execution guards around unmodified Emmet adapters/pipeline."""

import copy
import logging
import json
import math
import threading
from urllib.parse import urlsplit

import httpx
from profiling.llm import OpenRouterTagExtractor as UpstreamExtractor
from profiling.ports import ResultRefused
from profiling.types import RunStatus, ErrorCode

log = logging.getLogger(__name__)


# Guards that make the model a required, cost-capped dependency (see profiler_runtime).
REQUIRED_GUARDS = ("provider-key-limits",)


def official(url):
    p = urlsplit(str(url))
    return (
        p.scheme == "https"
        and p.hostname == "openrouter.ai"
        and p.port in (None, 443)
        and not p.username
        and not p.password
        and not p.query
        and not p.fragment
        and p.path.rstrip("/") in ("/api/v1", "/api/v1/chat/completions")
    )


class PreflightTransport(httpx.BaseTransport):
    """provider-key-limits mode: same destination and generation checks, no central reservation."""

    def __init__(self, transport=None):
        self.upstream = transport or httpx.HTTPTransport(retries=0)
        self.preflight = None

    def handle_request(self, request):
        if not official(request.url) or request.url.path != "/api/v1/chat/completions":
            raise httpx.TransportError("Unapproved provider destination")
        if self.preflight is not None:
            try:
                self.preflight()
            except Exception:
                raise httpx.TransportError("Generation validation failed before send") from None
        response = self.upstream.handle_request(request)
        response.read()
        if response.status_code >= 300:
            status = response.status_code
            response.close()
            # Same redaction as the guarded path: provider originals never reach logs.
            return httpx.Response(
                status, json={"error": {"code": "PROVIDER_REQUEST_FAILED"}}, request=request
            )
        return response

    def close(self):
        self.upstream.close()


class ProviderLimitedExtractor(UpstreamExtractor):
    """Any configured model; spend is capped by the provider key's own limit."""

    def __init__(self, base_url, api_key, model, *, timeout_s=120, seed=42, transport=None, credential_source=None):
        if not official(base_url) or not str(model or "").strip():
            raise ValueError("Official provider and an explicit model are required")
        self.guard_transport = PreflightTransport(transport)
        super().__init__(
            base_url,
            api_key,
            model,
            timeout_s=timeout_s,
            seed=seed,
            transport=self.guard_transport,
            credential_source=credential_source,
        )

    def bind_generation(self, check):
        self.guard_transport.preflight = check


class ExecutionState(threading.local):
    failure = None


class RequiredStore:
    """Refuse a RESULT_READY only when the catalog generation changed during analysis (stale ranking).

    Design CO-01 (BE position, 11-08 user decision A1): a valid recommendation produced through the fallback chain
    (v3 -> v2 -> no tags, or ranking without embedding similarity) is SUCCEEDED. Model/embedding adapters are still
    required at startup (/ready); a runtime model or embedding failure only degrades the ranking and is logged.
    """

    def __init__(self, store, state, binding=None):
        self.store = store
        self.state = state
        self.binding = binding

    def __getattr__(self, name):
        return getattr(self.store, name)

    def save(self, outcome):
        status = str(outcome.status)
        if status == "RUNNING":
            self.state.failure = None
        if status == "RESULT_READY" and self.binding is not None:
            try:
                self.binding.check()
            except Exception:
                self.state.failure = "generation_changed"
        if status == "RESULT_READY" and self.state.failure == "generation_changed":
            failed = copy.copy(outcome)
            failed.status = RunStatus.FAILED
            failed.search = None
            failed.validation = None
            failed.failure_code = ErrorCode.PIPELINE_ERROR
            failed.failure_reason = "Catalog generation changed during analysis"
            failed.callback_attempts = 0
            # Upstream intentionally does not retry a failed RESULT_READY store write. Persist the refusal first.
            self.store.save(failed)
            raise ResultRefused("Catalog generation changed during analysis")
        if status == "RESULT_READY" and (self.state.failure or outcome.tag_status not in ("ok", "empty", "skip", "skip_no_signal")):
            log.warning("profile recipient=%s fallback result accepted (tag_status=%s, execution=%s)",
                        getattr(outcome, "recipient_user_id", None), outcome.tag_status, self.state.failure or "ok")
        return self.store.save(outcome)
