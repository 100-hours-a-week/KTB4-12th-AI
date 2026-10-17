"""Async Chat/Profile client; only httpx is required, with no implicit retries."""

from typing import Any, Literal

import httpx


class SearchAPIError(httpx.HTTPStatusError):
    """HTTP status error with the API's machine code and validation issues.

    It remains catchable as httpx.HTTPStatusError. Unknown/non-JSON upstream
    errors preserve the HTTP response and expose code=None, issues=[].
    """

    def __init__(self, message: str, *, request: httpx.Request, response: httpx.Response):
        super().__init__(message, request=request, response=response)
        self.code: str | None = None
        self.issues: list[dict[str, Any]] = []
        try:
            body = response.json()
        except ValueError:
            return
        error = body.get('error') if isinstance(body, dict) else None
        if isinstance(error, dict):
            if isinstance(error.get('code'), str):
                self.code = error['code']
            if isinstance(error.get('issues'), list):
                self.issues = [issue for issue in error['issues'] if isinstance(issue, dict)]


def _result(response: httpx.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SearchAPIError(str(exc), request=exc.request, response=exc.response) from exc
    return response.json()


class SearchClient:
    """Reuse per lifespan. SearchAPIError exposes HTTP failures; transport errors propagate."""

    def __init__(
        self,
        base_url: str = 'http://127.0.0.1:4325',
        *,
        source: Literal['chat', 'profile'] = 'profile',
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if source not in ('chat', 'profile'):
            raise ValueError('source must be chat or profile')
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={'X-Search-Source': source},
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> 'SearchClient':
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.__aexit__(*exc)

    async def close(self) -> None:
        await self._http.aclose()

    async def ready(self) -> dict[str, Any]:
        response = await self._http.get('/readyz')
        return _result(response)

    async def metadata(self) -> dict[str, Any]:
        response = await self._http.get('/v1/metadata')
        return _result(response)

    async def search(
        self, request: dict[str, Any], *, snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(request)
        if snapshot_id is not None:
            payload['snapshotId'] = snapshot_id
        response = await self._http.post('/v1/search', json=payload)
        return _result(response)

    async def get_products(
        self, ids: list[int], *, snapshot_id: str,
    ) -> dict[str, Any]:
        response = await self._http.post(
            '/v1/products', json={'ids': ids, 'snapshotId': snapshot_id},
        )
        return _result(response)
