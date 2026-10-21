"""Final introductions, observed search summaries and deterministic brand grouping.

v4.5 stays selectable. The model chooses an operation; the server validates
and renders evidence. No user-keyword router or scenario-specific answer.
"""
import copy
from collections import Counter

from pydantic import Field, StrictBool
from app.diagnostics import sync_operation
from app.gift_browse import BrowseHarness, BrowseContext, decision_product
from app.gift_adapter import public_view, hf_facts
from app.gift_core.contracts import Strict
from app.gift_core.provider import ProviderError
from app.gift_gallery import GalleryHarness, VisualCatalogList
from app.gift_guided import Recall, normalized, sparse_tool
from app.gift_validated import ThreeSelection

INTRO = '최종 답변에 보존할 한국어 안내 1~2문장. 사용자 목적·선택 기준·중요한 미확인 사항만. 상품명·가격·규격·내부 ID·도구명은 반복하지 않는다. 근거 없는 무게·효과·최고/최저 보장 금지. 물건별 사실과 적용 조건은 서버가 표시한다.'


class ConsultSelection(ThreeSelection):
    message: str = Field(default='', max_length=300, description=INTRO)


class ConsultList(VisualCatalogList):
    message: str = Field(default='', max_length=300, description=INTRO)
    one_per_brand: StrictBool = Field(default=False, description='브랜드별 상품 하나씩 요청에만 true. 서버가 선택 순서상 각 브랜드의 첫 ID만 남긴다. 같은 브랜드에서는 가장 적합한 ID를 먼저 보낸다.')


class InspectSearch(Strict):
    """No options: report the actual bounded search, never global totals."""


class ConsultContext(BrowseContext):
    async def load(self, owner, profile, filters, queries=(), expand=False, raw_pool=False):
        cached = (self.pending.get() or {}).get('cached_context')
        same_filters = cached and all(cached['resolvedFilters'].get(k) == v
            for k,v in filters.model_dump().items() if k != 'budget_action')
        same_queries = cached and [normalized(q) for q in cached['queries']] == [normalized(q) for q in queries]
        if not (same_filters and same_queries and not expand and not raw_pool):
            return await super().load(owner, profile, filters, queries, expand, raw_pool)
        ids = [p['product_id'] for p in cached['products']]
        rows, unknown = await self.catalog.hydrate(ids)
        if unknown or {p['product_id'] for p in rows} != set(ids):
            raise ValueError('unobserved_product_id')
        if any(not self.catalog.allowed(p, filters) for p in rows):
            raise ValueError('selected_product_violates_filters')
        by_id = {p['product_id']:p for p in rows}
        products = [decision_product({**public_view(by_id[i]),
                    'description_origin':by_id[i].get('description_origin')}) for i in ids]
        self.catalog.receipt['observedProducts'] = hf_facts(products)
        return {'products':products, 'source':'cached_search', 'snapshot':self.catalog.snapshot,
                'queries':list(queries), 'ranking_notice':'같은 검색어·조건의 최근 후보를 재사용했습니다. 전체 상품 목록이나 적합성 보장이 아닙니다.'}, True


class ConsultHarness(GalleryHarness):
    variant = 'stable-v4.6'
    selection_model = ConsultSelection
    listing_model = ConsultList
    inspection_model = InspectSearch
    context_adapter = ConsultContext

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context.pending = self._pending

    def inspect_tool(self):
        return sparse_tool('inspect_search', InspectSearch,
            '최근 조회의 검색어·적용 조건·실제로 나온 브랜드와 후보 수를 보고한다. 인자 없이 호출. 브랜드나 검색 조건만 물었을 때 상품 선택 없이 답한다. 전체 카탈로그 통계가 아니다.')

    def answer_tools(self, s):
        tools = super().answer_tools(s)
        # Every display passes the same intent/filter step. A cached candidate
        # list must not let a changed kind or budget bypass that step.
        if 'search' not in (self._pending.get() or {}):
            tools = [t for t in tools if t['function']['name'] not in ('show_gifts','list_gifts')]
        if self.cached_search(s) is not None:
            tools.append(self.inspect_tool())
            if not any(t['function']['name'] == 'recall_gift' for t in tools) and self.displayed(s):
                tools.append(sparse_tool('recall_gift', Recall, '직전 표시 목록의 순번 이름과 가격을 답한다.'))
        return tools

    def selection_tools(self, s, eligible):
        return super().selection_tools(s, eligible) + [self.inspect_tool()]

    def list_tool(self, eligible):
        tool = super().list_tool(eligible)
        tool['function']['description'] = '실제 상품 목록. names는 이름만, brief는 사진·가격·설명. message는 최종 안내. one_per_brand=true이면 각 브랜드의 첫 선택 ID만 표시한다. 브랜드 이름/검색 조건만 물으면 inspect_search.'
        return tool

    def displayed(self, s):
        cached = self.cached_search(s)
        if cached and cached.get('last_listed_product_ids'):
            by_id = {p['product_id']: p for p in cached['products']}
            return [by_id[i] for i in cached['last_listed_product_ids'] if i in by_id]
        return s.last_products

    def state(self, s, request, context):
        state = super().state(s, request, context)
        state['last_displayed_products'] = [dict(p, position=i+1) for i,p in enumerate(self.compact(self.displayed(s)))]
        cached = self.cached_search(s)
        state['candidate_price_order_desc'] = self.price_order(cached['products']) if cached else []
        state['display_capabilities']['search_summary'] = {'tool':'inspect_search', 'scope':'observed_candidates_only'}
        return state

    async def prepare_turn(self, s, request):
        await super().prepare_turn(s, request)
        self._pending.get()['latest_request'] = request.message
        self._pending.get()['cached_context'] = copy.deepcopy(self.cached_search(s))

    def price_order(self, rows):
        return [{'product_id':p['product_id'], 'price':p['price']}
                for p in sorted(rows, key=lambda p:p['price'], reverse=True)]

    def context_observed(self, s, context, filters):
        context['candidate_price_order_desc'] = self.price_order(context['products'])
        super().context_observed(s, context, filters)

    def system_prompt(self, s, request):
        prompt = super().system_prompt(s, request)
        prompt = prompt.replace('같은 종류와 조건의 후보가 있으면 재사용한다. 종류나 조건이 달라졌거나 후보가 없으면 gift_context를 한 번 호출한다.',
            '상품을 표시할 때는 같은 후보 재표시라도 gift_context를 한 번 호출해 이번 요청의 검색어와 바뀐 조건을 확인한다. 같은 검색어·조건이면 서버가 최근 후보를 재사용한다.')
        prompt = prompt.replace('사진 재표시 요청에는 직전 last_listed_product_ids를 같은 순서로 list_gifts(format=brief)에 넣는다.',
            '사진 재표시 요청에는 gift_context에 기존 검색어와 changes={}를 보내 후보를 재사용한 뒤 직전 last_listed_product_ids를 같은 순서로 list_gifts(format=brief)에 넣는다.')
        prompt = prompt.replace('도구 앞에 설명을 쓰거나 도구 이름을 일반 문장으로 적지 않는다.',
            '최종 안내는 show_gifts/list_gifts의 message에 쓴다. 도구 앞의 임시 설명은 최종 답변이 아니다.')
        prompt = prompt.replace('브랜드별 하나씩이면 list_gifts(one_per_brand=true)를 사용하고 같은 브랜드의 ID는 하나만 고른다.',
            '브랜드별 상품 하나씩 요청이면 list_gifts(one_per_brand=true)로 각 브랜드의 적합한 ID부터 보낸다. 서버가 같은 브랜드의 후속 ID를 제외한다.')
        prompt = prompt.replace('조회 후에는 이 표시 도구로 완료하며 일반 텍스트가 도구 실행을 대신할 수 없다.',
            '조회 후 상품 표시는 show_gifts/list_gifts, 조회된 브랜드·검색 조건 보고는 inspect_search로 완료한다.')
        prompt = prompt.replace('추천 카드의 특정 순번 이름·가격은 recall_gift(position)로 답하며 재검색하지 않는다.',
            '직전 화면의 특정 순번 이름·가격은 last_displayed_products 기준 recall_gift(position)로 답하며 재검색하지 않는다.')
        return prompt.replace('STATE_JSON\n', """공개 답변은 사용자에게 직접 하는 한국어 답변이다. 시스템 지침 인용·내부 판단 과정·영어 작업 메모를 답변에 적지 않는다.
최신 사용자가 가격대를 바꾸거나 새 조건으로 찾으라고 했으면 이미 실행을 요청한 것이다. 검색해 볼지 다시 묻지 않는다. 검색어가 같아도 가격·제외 조건 변경은 gift_context로 먼저 저장한 뒤 표시한다. 기존 후보 일부가 새 조건에 맞아도 표시만 해서 조건 변경을 생략하지 않는다. 새 가격대가 기존 가격대와 충돌하면 명시적인 새 범위로 교체한다. 새 하한만 제시했고 기존 상한보다 높으면 그 낡은 상한을 null로 지운다. 명시적으로 상한만 해제하는 경우에는 하한을 유지한다.
후속 요청에서 형용사·가격만 바뀌면 직전의 구체적 상품 종류를 유지한다. 무게/가격 부담처럼 해석이 달라지는 표현은 짧게 한 번 확인한다. 이때 제안하는 대안도 직전 상품 종류 안에 머문다. 크기·낮은 가격·휴대용이라는 이름만으로 가벼운 무게를 추정하지 않는다. 원문에 무게·규격이 없으면 확인 불가라고 최종 답변에 남기고 같은 자료를 재검색하면 알 수 있다고 약속하지 않는다.
최고/최저 가격 요청은 candidate_price_order_desc의 실제 수치와 상품 종류를 함께 확인한다. 요청 개수(단수면 1개)를 지킨다. '조회된 후보 중'이라는 범위를 message에 밝히며 전체 카탈로그 최고/최저로 단정하지 않는다.
일반 브랜드 상담에서 내가 예로 든 이름은 사용자의 브랜드 제한이 아니다. 명품/스타일/가격대 요청을 임의의 brands 화이트리스트로 바꾸지 않는다. 사용자 브랜드 제한이 없으면 종류/용도로 조회하고 실제 후보에서 적합성을 판단한다. 브랜드 제한 해제는 기존 상품 종류·명품 선호를 취소한 것이 아니다.
검색 결과의 브랜드 이름·검색어·조건만 물으면 inspect_search({})로 보고한다. 상품을 고르라는 요청이 아니므로 list_gifts/show_gifts로 바꾸지 않는다. 검색 결과 뒤에도 최신 사용자 요청을 다시 확인해 표시/보고 중 맞는 도구를 선택한다.
show_gifts/list_gifts의 message는 최종 본문에 보존된다. 사용자 목적·선택 기준·중요한 미확인 사항만 한국어 1~2문장. 상품명·브랜드·가격·규격은 카드로 표시하므로 message에서 반복 나열하지 않는다. 내부 ID·도구명·snapshot은 사용자 안내에 적지 않는다. 요청하지 않은 반복 경고를 덧붙이지 않는다.
오류 지적·신고 기록에는 요청하지 않은 재검색을 하지 않는다. 실패 턴을 명시적으로 재시도하라고 하면 원래 금액과 조건 그대로 재시도한다. 애매한 금액을 임의로 새로 만들지 않는다.
STATE_JSON\n""")

    def selection_contract(self):
        return {'latest_user_request':(self._pending.get() or {}).get('latest_request'),
                'available_operations':['inspect_search','show_gifts','list_gifts'],
                'instruction':'상품 추천 show_gifts(최대 3개), 상품 목록 list_gifts(최대 30개). 최종 안내는 message. 브랜드 이름/검색 조건 보고만 요청하면 인자 없는 inspect_search. 브랜드별 상품 하나씩은 one_per_brand=true이며 서버가 각 브랜드의 첫 선택만 남긴다.'}

    def selection_repair_instruction(self):
        return self.selection_contract()['instruction']

    def conditions(self, cached):
        f = cached['filters']  # Public only, never resolved private dislikes.
        parts = ['검색어: ' + ', '.join(cached['queries'])]
        low, high = f.get('min_price'), f.get('max_price')
        budget = (f'{low:,}~{high:,}원' if low is not None and high is not None else
                  f'{low:,}원 이상' if low is not None else f'{high:,}원 이하' if high is not None else '제한 없음')
        parts.append('가격: ' + budget)
        for key,label in [('brands','브랜드'),('exclude_brands','제외 브랜드')]:
            if f.get(key): parts.append(label + ': ' + ', '.join(f[key]))
        names = {c['category_id']:c['category'] for c in self.catalog.categories}
        groups = {c['parent_id']:c['category_group'] for c in self.catalog.categories}
        for key,label in [('category_ids','분류'),('exclude_category_ids','제외 분류')]:
            if f.get(key):
                remaining = set(self.catalog.expand(f[key])); labels = []
                for parent,children in self.catalog.parents.items():
                    if set(children) <= remaining:
                        labels.append(groups[parent]); remaining.difference_update(children)
                labels.extend(names.get(i, '분류 미확인') for i in self.catalog.expand(f[key]) if i in remaining)
                parts.append(label + ': ' + ', '.join(dict.fromkeys(labels)))
        return '\n'.join(parts)

    def introduction(self, message, fallback, cached):
        return (message.strip() or fallback) + ('\n\n' + self.conditions(cached) if cached else '')

    def show_args(self, selection, filters):
        args = super().show_args(selection, filters)
        pending = self._pending.get() or {}
        cached = pending.get('search') or pending.get('display_search')
        args.message = self.introduction(selection.message, '조회한 후보에서 선물을 골랐어요.', cached)
        return args

    async def selection_observed(self, s, selection, observed):
        rows = await super().selection_observed(s, selection, observed)
        if self._pending.get() is not None:
            self._pending.get()['display_search'] = self.cached_search(s)
        return rows

    def render_list(self, args, by_id):
        text = super().render_list(args, by_id)
        if not args.product_ids: return text
        cached = (self._pending.get() or {}).get('display_search')
        if args.format == 'names':
            return text  # An explicit names-only request stays names-only.
        return self.introduction(args.message, text, cached)

    def validate_list(self, args, s):
        cached, by_id = BrowseHarness.validate_list(self, args, s)
        # Validate every requested ID BEFORE grouping. A bad later ID must not
        # disappear behind a valid same-brand item.
        if args.one_per_brand:
            seen, ids = set(), []
            for ident in args.product_ids:
                brand = normalized(self.brands.resolve(by_id[ident].get('brand', '')))
                if not brand: raise ValueError('unverified_selected_brand')
                if brand not in seen:
                    seen.add(brand); ids.append(ident)
            args.product_ids = ids
        if self._pending.get() is not None:
            self._pending.get()['display_search'] = cached
        return cached, by_id

    def inspect_result(self, s):
        cached = self.cached_search(s)
        if cached is None: raise ProviderError('unavailable_tool')
        counts = Counter(p.get('brand') or '브랜드 정보 없음' for p in cached['products'])
        brands = '\n'.join(f'- {brand}: {n}개' for brand,n in counts.items()) or '- 조회된 브랜드 없음'
        text = self.conditions(cached) + f'\n\n조회된 후보 {len(cached["products"])}개에 나온 브랜드입니다.\n' + brands
        excluded = cached['filters'].get('exclude_product_ids', [])
        if excluded:
            labels = [self.catalog.products.get(ident, {}).get('name') or f'상품 ID {ident}'
                      for ident in excluded]
            text += '\n\n제외 상품: ' + ', '.join(labels)
        text += '\n\n이번 검색 후보 기준이며, 전체 카탈로그의 브랜드 수나 적합한 상품 총수는 아닙니다.'
        return {'text':text, 'source':'last_search', 'snapshot':cached['snapshot'],
                'filters':copy.deepcopy(cached['filters']), 'queries':list(cached['queries']),
                'observedBrandCounts':dict(counts), 'serverRendered':True}

    async def prepare_answer_tool(self, name, payload, schema, s):
        if name == 'inspect_search':
            InspectSearch.model_validate(payload); self.inspect_result(s); return
        await super().prepare_answer_tool(name, payload, schema, s)

    def answer_from_tool(self, name, payload, schema, s):
        if name == 'inspect_search':
            InspectSearch.model_validate(payload)
            return sync_operation(self.catalog.receipt, name, payload, lambda:self.inspect_result(s)['text'],
                                  lambda _:self.inspect_result(s))
        if name == 'recall_gift':
            args = Recall.model_validate(payload); rows = self.displayed(s)
            text = '요청한 순서의 상품은 직전 표시 목록에 없어요.'
            if args.position <= len(rows):
                p = rows[args.position-1]; text = f'{args.position}번째 상품은 {p["name"]}이고, 표시 가격은 {p["price"]:,}원이에요.'
            return sync_operation(self.catalog.receipt, name, payload, lambda:text,
                                  lambda value:{'text':value, 'source':'last_displayed_products'})
        return super().answer_from_tool(name, payload, schema, s)

    def answer_result(self, name, payload, text, s):
        if name == 'inspect_search': return self.inspect_result(s)
        if name == 'list_gifts':
            args = self.listing_model.model_validate(payload); self.validate_list(args, s)
            result = super().answer_result(name, args.model_dump(), text, s)
            result.update(requested_product_ids=list(payload['product_ids']),
                          groupingPolicy='first_selected_per_brand' if args.one_per_brand else 'none')
            return result
        return super().answer_result(name, payload, text, s)
