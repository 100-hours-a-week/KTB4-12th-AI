"""Allow a verified search list to become recommendation cards without re-search."""
import copy

from app.gift_browse import BrowseHarness
from app.gift_core.provider import ProviderError


class CachedCardHarness(BrowseHarness):
    variant = 'stable-v4.4'
    require_tool_after_repair = True

    def cached_search(self, s):
        cached = super().cached_search(s)
        if cached and 'search' not in (self._pending.get() or {}):
            public = s.filters.model_dump()
            public['exclude_category_ids'] = list(getattr(s, 'public_exclusions', public['exclude_category_ids']))
            # budget_action describes how the state was reached, not its limits.
            if any(cached.get('filters', {}).get(k) != v for k, v in public.items() if k != 'budget_action'):
                return None
        return cached

    def answer_tools(self, s):
        tools = super().answer_tools(s)
        cached = self.cached_search(s)
        if cached and cached['products']:
            tools.append(self.selection_tool(s, [p['product_id'] for p in cached['products']]))
        return tools

    async def selection_observed(self, s, selection, observed):
        if 'search' in (self._pending.get() or {}):
            return observed  # This turn already retrieved authoritative rows.
        cached = self.cached_search(s)
        if cached is None:
            raise ProviderError('unavailable_tool')
        rows = {p['product_id']: p for p in cached['products']}
        ids = selection.product_ids
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate_selected_id')
        if not set(ids) <= rows.keys():
            raise ValueError('unobserved_product_id')
        # Rehydrate only selected identities under the same snapshot. The web
        # adapter is new each turn; unselected cached IDs are not in its map.
        # This is an identity lookup, not another search or scope expansion.
        hydrated, unknown = await self.catalog.hydrate(ids)
        if unknown or {p['product_id'] for p in hydrated} != set(ids):
            raise ValueError('unobserved_product_id')
        self.catalog.receipt['selectionSource'] = {'source': 'last_search', 'snapshot': cached['snapshot'], 'product_ids': list(ids)}
        return copy.deepcopy(rows)

    def system_prompt(self, s, request):
        prompt = super().system_prompt(s, request)
        prompt = prompt.replace('새 추천과 조건 변경은 gift_context를 한 번 호출한다.',
            '상품 종류나 조건이 달라지면 gift_context를 한 번 호출한다. 이미 조회한 같은 조건의 후보에서 추천을 고를 때는 last_search를 사용한다.')
        prompt = prompt.replace('이 결과는 과거 기록이므로 새 추천의 조회를 대체하지 않는다.',
            '유효한 last_search가 있으면 같은 조건에서 후보를 고를 때 재사용한다. 조건이나 상품 종류가 바뀌면 새로 조회한다.')
        return prompt.replace('STATE_JSON\n', '''조회한 목록에서 몇 개를 추천해 달라는 후속 요청은 show_gifts로 카드 표시한다. 목록은 list_gifts, 추천 카드는 show_gifts다. 같은 조건의 last_search가 있으면 재검색 없이 바로 선택할 수 있다. 선택 상품은 서버가 같은 snapshot에서 다시 확인한다.
실패한 도구 호출 뒤에는 안내된 스키마로 한 번 고쳐 호출한다. 도구가 없다고 설명하며 일반 텍스트 추천으로 대체하지 않는다. product_ids는 JSON 배열이며, 배열을 따옴표로 감싼 문자열이 아니다.
STATE_JSON\n''')

    def feedback(self, exc):
        if str(exc) == 'unavailable_tool':
            return {'code': 'unavailable_tool', 'hint': '현재 제공된 도구 목록을 확인하세요. show_gifts/list_gifts가 있으면 그 후보 ID로 호출하고, 없다면 gift_context로 먼저 조회하세요. 일반 텍스트로 도구 실행을 대신하지 마세요.'}
        value = super().feedback(exc)
        if value and value.get('code') == 'invalid_tool_arguments':
            if any(f.get('location') == 'product_ids' and f.get('type') == 'list_type' for f in value.get('fields', [])):
                value['hint'] = 'product_ids는 문자열이 아닌 JSON 배열이어야 합니다. 예: {"product_ids":["실제 후보 ID"]}. 현재 제공된 스키마로 한 번 수정해 호출하세요.'
        return value
