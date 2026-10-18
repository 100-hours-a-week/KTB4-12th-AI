"""Cost and mandatory-execution guards around unmodified Emmet adapters/pipeline."""

import copy
import json
import math
import threading
from urllib.parse import urlsplit

import httpx
from profiling.llm import OpenRouterTagExtractor as UpstreamExtractor
from profiling.types import RunStatus, ErrorCode


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


class BudgetTransport(httpx.BaseTransport):
    def __init__(self, budget, transport=None):
        if budget is None:
            raise ValueError("Central budget adapter required")
        self.budget = budget
        self.upstream = transport or httpx.HTTPTransport(retries=0)
        self.preflight = None

    def handle_request(self, request):
        if not official(request.url) or request.url.path != "/api/v1/chat/completions":
            raise httpx.TransportError("Unapproved provider destination")
        if self.preflight is not None:
            try:
                self.preflight()
            except Exception:
                raise httpx.TransportError(
                    "Generation validation failed before reservation"
                ) from None
        body = json.loads(request.content)
        if body.get("model") != "openai/gpt-6-luna":
            raise httpx.TransportError("Unapproved integration model")
        body.pop("temperature", None)
        body["reasoning"] = {"effort": "low", "exclude": True}
        try:
            body, ident = self.budget.reserve(body)
        except Exception:
            raise httpx.TransportError("Central reservation not acknowledged") from None
        cost = None
        try:
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding")
            }
            response = self.upstream.handle_request(
                httpx.Request(
                    request.method,
                    request.url,
                    headers=headers,
                    json=body,
                    extensions=dict(request.extensions),
                )
            )
            response.read()
            if response.status_code == 200:
                try:
                    payload = response.json()
                    usage = payload.get("usage", {})
                    raw_cost = usage.get("cost")
                    cost = (
                        raw_cost
                        if type(raw_cost) in (int, float)
                        and math.isfinite(raw_cost)
                        and raw_cost >= 0
                        else None
                    )
                    if raw_cost is not None and cost is None:
                        payload["usage"] = {
                            k: v for k, v in usage.items() if k != "cost"
                        }
                        response.close()
                        response = httpx.Response(200, json=payload, request=request)
                except (ValueError, TypeError, AttributeError):
                    pass
            if response.status_code >= 300:
                status = response.status_code
                response.close()
                # Upstream core includes error response text in its error. Do not forward provider originals into logs.
                return httpx.Response(
                    status,
                    json={"error": {"code": "PROVIDER_REQUEST_FAILED"}},
                    request=request,
                )
            return response
        finally:
            self.budget.finish(ident, cost)

    def close(self):
        self.upstream.close()
        self.budget.close()


class BudgetedExtractor(UpstreamExtractor):
    def __init__(
        self,
        base_url,
        api_key,
        model,
        *,
        budget,
        timeout_s=120,
        seed=42,
        transport=None,
    ):
        if not official(base_url) or model != "openai/gpt-6-luna":
            raise ValueError("Only the pinned existing provider/model is supported")
        self.guard_transport = BudgetTransport(budget, transport)
        super().__init__(
            base_url,
            api_key,
            model,
            timeout_s=timeout_s,
            seed=seed,
            transport=self.guard_transport,
        )

    def bind_generation(self, check):
        self.guard_transport.preflight = check


class ExecutionState(threading.local):
    failure = None


class RequiredStore:
    """Reject degraded RESULT_READY before its durable commit or callback; leave core ranking unchanged."""

    def __init__(self, store, state, binding=None, required_model=True):
        self.store = store
        self.state = state
        self.binding = binding
        self.required_model = required_model

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
        if status == "RESULT_READY" and (
            self.state.failure
            or self.required_model
            and outcome.tag_status not in ("ok", "empty", "skip", "skip_no_signal")
        ):
            failed = copy.copy(outcome)
            failed.status = RunStatus.FAILED
            failed.search = None
            failed.validation = None
            failed.failure_code = ErrorCode.PIPELINE_ERROR
            failed.failure_reason = (
                "Required model/embedding execution was not verified"
            )
            failed.callback_attempts = 0
            # Upstream intentionally does not retry a failed RESULT_READY store write. Persist the refusal first.
            self.store.save(failed)
            raise RuntimeError("Required model/embedding execution was not verified")
        return self.store.save(outcome)
