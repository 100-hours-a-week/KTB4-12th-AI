"""Separate recommendation cards from grounded catalog lists.

One bounded search snapshot survives turns independently of displayed cards.
Only a completed turn commits it; requests remain model decisions, not a router.
"""
import copy
import difflib
import hashlib
import json
from typing import Literal

from pydantic import Field, ValidationError
from app.gift_core.contracts import Strict, Filters
from app.gift_core.provider import ProviderError
from app.gift_guided import SearchChanges, normalized, sparse_tool
from app.gift_stable import catalog_explanation, RankedContext
from app.gift_validated import ThreeGiftHarness
from app.diagnostics import sync_operation


class CatalogList(Strict):
    product_ids: list[str] = Field(max_length=30, description='사용자가 개수를 정했으면 그 수만큼, 적합한 후보가 부족하면 그보다 적게 선택한다. 각 상품명을 읽고 실제 물건의 종류가 사용자 요청과 일치하는 ID만 고른다. 검색 순위나 enum에 있다는 이유로 전부 넣지 않는다. 부품·액세서리·다른 물건은 그 종류를 요청한 경우에만 포함. 표시 순서대로 최대 30개.')
    format: Literal['names', 'brief'] = Field(default='names', description='names는 상품명만, brief는 상품명·가격·카탈로그 설명. 이름만 요청하면 names.')


LIST = sparse_tool('list_gifts', CatalogList, '상품명/모델명 나열, 나머지 목록, 여러 상품의 간단 설명에 사용한다. 호출 전에 후보별 상품명을 읽고 요청한 실제 종류와 맞는지 판정하여 적합한 ID만 고른다. 검색 후보에는 무관한 물건과 부속품이 섞여 있다. 서버는 선택된 ID의 원문을 그대로 표시하며 종류를 대신 판단하지 않는다.')


def decision_product(row):
    """Do not amplify a catalog classification into independent evidence.

    Keep category IDs for explicit filters, but choose identity from the title
    and independently sourced descriptions. Raw catalog rows stay untouched.
    """
    row = copy.deepcopy(row)
    row['category'] = ''
    if str(row.get('description_origin') or '').startswith('COMPOSED_'):
        row['description'] = ''
    return row


class BrowseContext(RankedContext):
    async def load(self, *args, **kwargs):
        context, cached = await super().load(*args, **kwargs)
        for row in context['products']:
            row['description_origin'] = self.catalog.products[row['product_id']].get('description_origin')
        context['products'] = [decision_product(row) for row in context['products']]
        return context, cached


class BrowseHarness(ThreeGiftHarness):
    variant = 'stable-v4.3'
    listing_model = CatalogList
    context_adapter = BrowseContext
    max_model_calls = 4
    max_tool_repairs = 2  # At most one before search and one after search.

    def compact(self, rows):
        return [decision_product({**p, 'description_origin': row.get('description_origin')})
                for p, row in zip(super().compact(rows), rows)]

    def explanation(self, product):
        return catalog_explanation(decision_product(product))

    def scope_key(self, s, recipient_scope):
        value = {'profile': s.profile.model_dump(), 'recipient_scope': recipient_scope}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def cached_search(self, s):
        pending = self._pending.get() or {}
        if 'search' in pending:
            return pending['search']
        cached = getattr(s, 'last_search', {})
        if (cached and cached.get('snapshot') == self.catalog.snapshot
                and cached.get('scopeKey') == self.scope_key(s, s.filters.recipient_scope)):
            return cached
        return None

    def context_observed(self, s, context, filters):
        pending = self._pending.get()
        if pending is not None:
            pending['search'] = {
                'snapshot': context['snapshot'], 'source': context['source'],
                'queries': list(context.get('queries', [])), 'filters': copy.deepcopy(context['filters']),
                'resolvedFilters': filters.model_dump(),
                'scopeKey': self.scope_key(s, filters.recipient_scope),
                'products': copy.deepcopy(context['products'][:30]), 'last_listed_product_ids': [],
            }

    def state(self, s, request, context):
        state = super().state(s, request, context)
        cached = self.cached_search(s)
        state['last_search'] = None if cached is None else {
            'snapshot': cached['snapshot'], 'source': cached['source'], 'queries': cached['queries'], 'filters': cached['filters'],
            'products': [decision_product(row) for row in cached['products']],
            'retrieved_count': len(cached['products']),
            'last_listed_product_ids': list(cached.get('last_listed_product_ids', [])),
            'notice': '최근 조회한 후보이며 전체 카탈로그나 적합 상품의 총수가 아닙니다. last_products는 실제 추천 카드, last_listed_product_ids는 직전 텍스트 목록의 표시 순서입니다.',
        }
        return state

    def system_prompt(self, s, request):
        prompt = super().system_prompt(s, request)
        prompt = prompt.replace('새 추천과 조건 변경은 gift_context를 한 번 호출한 뒤 show_gifts로 마친다.',
            '새 추천과 조건 변경은 gift_context를 한 번 호출한다. 추천 카드는 show_gifts, 상품명 목록이나 간단 비교 목록은 list_gifts로 마친다.')
        prompt = prompt.replace('조회가 끝나면 현재 show_gifts만 사용할 수 있다.',
            '조회가 끝나면 요청에 따라 show_gifts 또는 list_gifts로 마친다. 카드 추천은 최대 세 개지만 텍스트 목록은 조회한 후보 안에서 최대 30개까지 가능하다.')
        prompt = prompt.replace('사용자 요청 수만큼 최대 세 ID를 선택한다.',
            'show_gifts에서는 사용자 요청 수만큼 최대 세 ID를 선택한다.')
        prompt = prompt.replace('이전 추천의 상품명·가격은 last_products의 화면 순서를 사용해 recall_gift(position)를 호출한다.',
            '이전 추천 카드의 특정 상품명·가격은 last_products의 화면 순서를 사용해 recall_gift(position)를 호출한다.')
        prompt = prompt.replace('잘못된 호출은 오류와 현재 도구 스키마를 보고 한 번만 수정한다.',
            '잘못된 호출은 오류와 현재 도구 스키마를 보고 조회 단계와 표시 단계에서 각각 한 번만 수정한다.')
        return prompt.replace('STATE_JSON\n', '''사용자가 검색 결과·모델명·나머지 후보 목록을 요청하면 last_search의 상품명과 설명을 읽고 해당 종류의 ID를 list_gifts로 나열한다. 이름만 원하면 format=names, 간단 설명도 원하면 brief다. 4개 이상을 비교하거나 보여 달라는 요청은 list_gifts로 처리한다. 사용자가 개수를 정하면 선택 ID 수를 그 수에 맞추고 초과하지 않는다. 적합한 후보가 부족하면 그보다 적게 고른다. 모델명을 기억으로 만들지 않는다.
last_search는 실제 검색한 후보이고 last_products는 그중 추천 카드로 고른 상품이다. 나머지 후보는 부적합으로 확정된 것이 아니며, 검색 후보 수는 해당 종류의 총수가 아니다. 부품·액세서리·다른 종류를 목록에 섞지 않는다. 나머지를 요청하면 직전 목록(없으면 추천 카드)에 이미 표시한 ID는 빼고 고른다.
조회 결과를 전부 복사하지 않는다. ID별 상품명을 먼저 읽어 요청한 실제 물건인지 확인한다. 분류에서 자동 생성한 설명과 분류명은 종류 판단에 중복 근거가 되지 않도록 생략되어 있다. category_id는 필터용 색인이지 적합성 보장이 아니다. 상품명에 명시된 실제 종류를 우선하고, 요청과 다른 후보는 제외한다. 모델명 목록에서는 다른 물건과의 혼합 묶음도 사용자가 원한 경우에만 넣는다.
검색한 후보가 남아 있으면 다시 검색할 필요 없이 list_gifts를 쓴다. last_search가 없거나 다른 조건의 새 검색이 필요하면 gift_context 후 list_gifts로 답한다. 사용자가 이미 목록이나 검색을 요청했다면 다시 진행 허락을 묻지 않는다. 일반 텍스트로 도구를 약속하고 끝내지 않는다.
브랜드 오류 피드백은 필드 경로·거부된 값·카탈로그 후보를 보여준다. 현재 요청 원문과 비교해 정확한 브랜드명으로 한 번 수정한다. 사용자 동의 없이 기존 가격 제한을 없애거나 늘리지 않는다.
STATE_JSON\n''')

    def list_tool(self, eligible):
        tool = copy.deepcopy(LIST)
        tool['function']['parameters']['properties']['product_ids']['items']['enum'] = list(eligible)
        return tool

    def answer_tools(self, s):
        tools = super().answer_tools(s)
        cached = self.cached_search(s)
        if cached and cached['products']:
            tools.append(self.list_tool(p['product_id'] for p in cached['products']))
        return tools

    def selection_tools(self, s, eligible):
        return [self.selection_tool(s, eligible), self.list_tool(eligible)]

    def selection_contract(self):
        return {'available_operations': ['show_gifts', 'list_gifts'],
                'instruction': '추천 카드는 show_gifts(최대 3개), 상품명·나머지·4개 이상 목록은 list_gifts(최대 30개). 적합한 실제 후보 ID만 선택하세요. 이름만이면 names, 간단 설명도 원하면 brief. 적합한 후보가 없으면 product_ids=[]로 해당 도구를 호출하세요. 서버가 원문 또는 적합한 후보 없음 안내를 표시합니다.'}

    def selection_repair_instruction(self):
        return '조회가 완료됐습니다. 요청이 추천 카드이면 show_gifts, 이름·모델명·간단 설명·여러 후보 목록이면 list_gifts를 호출하세요. 실제 후보 ID만 선택하고, 적합한 후보가 없으면 product_ids=[]로 호출하세요. 안내 문구는 서버가 표시합니다.'

    def validate_list(self, args, s):
        cached = self.cached_search(s)
        if cached is None:
            raise ProviderError('unavailable_tool')
        ids = args.product_ids
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate_selected_id')
        by_id = {p['product_id']: p for p in cached['products']}
        if not set(ids) <= by_id.keys():
            raise ValueError('unobserved_product_id')
        filters = Filters.model_validate(cached['resolvedFilters'])
        if any(not self.catalog.allowed(by_id[i], filters) for i in ids):
            raise ValueError('selected_product_violates_filters')
        return cached, by_id

    def render_list(self, args, by_id):
        if not args.product_ids:
            return '조회한 후보 중 요청한 종류에 맞춰 더 나열할 상품은 없어요.'
        lines = []
        for index, ident in enumerate(args.product_ids, 1):
            row = by_id[ident]
            line = f"{index}. {row['name']}"
            if args.format == 'brief':
                line += f" — {row['price']:,}원"
                explanation, evidence = self.explanation(row)
                if evidence['field'] == 'description':
                    line += f"\n   {explanation}"
            lines.append(line)
        return '\n'.join(lines)

    def answer_from_tool(self, name, payload, schema, s):
        if name != 'list_gifts':
            return super().answer_from_tool(name, payload, schema, s)
        args = self.listing_model.model_validate(payload)
        cached, by_id = self.validate_list(args, s)
        ids = args.product_ids
        text = sync_operation(self.catalog.receipt, name, args.model_dump(), lambda: self.render_list(args, by_id),
            lambda value: {'text': value, 'source': 'last_search', 'snapshot': cached['snapshot'],
                           'product_ids': ids, 'serverRendered': True})
        self._pending.get()['listed'] = list(ids)
        return text

    def answer_result(self, name, payload, text, s):
        if name != 'list_gifts':
            return super().answer_result(name, payload, text, s)
        return {'text': text, 'source': 'last_search', 'snapshot': self.cached_search(s)['snapshot'],
                'product_ids': list(payload['product_ids']), 'serverRendered': True}

    def commit_history(self, s, request, text, cards, executed, turn_id):
        super().commit_history(s, request, text, cards, executed, turn_id)
        pending = self._pending.get() or {}
        cached = pending.get('search') or self.cached_search(s)
        if cached is not None:
            s.last_search = copy.deepcopy(cached)
            if 'listed' in pending:
                s.last_search['last_listed_product_ids'] = list(pending['listed'])

    def parse_context(self, payload, schema, s, request):
        args = SearchChanges.model_validate(payload)
        errors = []
        for field in ('brands', 'exclude_brands'):
            for index, value in enumerate(args.changes.model_dump(exclude_unset=True).get(field, [])):
                resolved = self.brands.resolve(value)
                if normalized(resolved) not in self.brands.exact:
                    errors.append({'field': f'changes.{field}[{index}]', 'received': value,
                        'catalog_candidates': difflib.get_close_matches(value, self.catalog.brand_names, n=3, cutoff=.5)})
        if self._pending.get() is not None:
            self._pending.get()['brand_errors'] = errors
        return super().parse_context(payload, schema, s, request)

    def feedback(self, exc):
        if str(exc) == 'unknown_catalog_brand':
            return {'code': 'unknown_catalog_brand', 'invalid_fields': (self._pending.get() or {}).get('brand_errors', []),
                    'hint': '검색은 실행되지 않았습니다. received는 방금 보낸 잘못된 값입니다. 현재 사용자 원문과 catalog_candidates를 비교해 해당 필드만 정확히 고쳐 gift_context를 다시 호출하세요. 같은 오타를 반복하거나 필터를 임의 삭제하지 마세요.'}
        if isinstance(exc, ValidationError):
            allowed = {'product_ids', 'format', 'position'}
            if all(all(isinstance(x, int) or x in allowed for x in e['loc']) for e in exc.errors()):
                return {'code': 'invalid_tool_arguments', 'fields': [
                    {'location': '.'.join(map(str, e['loc'])), 'type': e['type']}
                    for e in exc.errors(include_input=False, include_context=False, include_url=False)[:8]],
                    'hint': '추천 카드(show_gifts)는 최대 3개, 텍스트 목록(list_gifts)은 최대 30개입니다. 요청한 표시 방식의 도구와 실제 후보 ID를 사용하세요.'}
        if str(exc) in ('unavailable_tool', 'tool_budget_exhausted'):
            return {'code': str(exc), 'hint': self.selection_repair_instruction()}
        return super().feedback(exc)
