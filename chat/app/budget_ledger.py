"""Cost-only connection to the existing cumulative task ledger; never imports it."""
from urllib.parse import urlsplit

import httpx

from app.errors import APIError
from app.task_budget import ACCOUNT


class BudgetLedger:
    def __init__(self, url, token, *, transport=None):
        target = urlsplit(url)
        if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.username or target.password or target.query or target.fragment or target.path not in ('', '/') or not token:
            raise ValueError('An authenticated existing-ledger loopback gateway is required')
        self.http = httpx.AsyncClient(base_url=url, headers={'Authorization': 'Bearer '+token}, timeout=20, follow_redirects=False, trust_env=False, transport=transport)

    async def call(self, method, **args):
        if method not in {'budget_summary', 'budget_reserve', 'budget_finish'} or args.get('account') != ACCOUNT:
            raise APIError(400, '허용되지 않는 원장 작업입니다.')
        try:
            response = await self.http.post('/rpc', json={'method': method, 'args': args})
            if response.status_code != 200:
                raise APIError(429 if response.status_code == 429 else 503, '공유 예산 예약 또는 조회를 확인하지 못했습니다.')
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Invalid ledger response envelope')
            result = payload['result']
        except (httpx.HTTPError, ValueError, KeyError):
            raise APIError(503, '기존 공유 비용 원장에 연결할 수 없습니다.') from None
        if method == 'budget_summary':
            if not isinstance(result, dict) or result.get('limitUsd') != 5:
                raise APIError(503, '기존 누적 5달러 원장을 확인하지 못했습니다.')
        elif result is not True:
            raise APIError(503, '공유 원장의 예약·정산 확인이 없습니다.')
        return result

    async def close(self):
        await self.http.aclose()
