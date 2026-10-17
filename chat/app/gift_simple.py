"""v4.8: product filters only; recipient identity belongs to the application.

Keep v4.6 and the flat v4.7 experiment reproducible. This version retains v4.6
sparse updates while removing delivery and recipient controls from model input.
"""
import copy

from pydantic import Field

from app.gift_consult import ConsultHarness
from app.gift_core.contracts import Strict
from app.gift_guided import sparse_tool


class ProductFilterChanges(Strict):
    min_price: int | None = Field(default=None, ge=0, le=1_000_000_000, description='변경할 최저가. null은 최저가 삭제. 생략은 유지.')
    max_price: int | None = Field(default=None, ge=0, le=1_000_000_000, description='변경할 최고가. null은 최고가 삭제. 생략은 유지.')
    brands: list[str] = Field(default_factory=list, max_length=20, description='포함 브랜드 목록 교체. []는 포함 제한 삭제. 생략은 유지.')
    exclude_brands: list[str] = Field(default_factory=list, max_length=20, description='제외 브랜드 목록 교체. []는 제외 제한 삭제. 생략은 유지.')
    category_ids: list[str] = Field(default_factory=list, max_length=20, description='명시적인 카테고리 제한만 지정. 일반 상품 종류는 queries로 검색.')
    exclude_category_ids: list[str] = Field(default_factory=list, max_length=20)
    exclude_product_ids: list[str] = Field(default_factory=list, max_length=30, description='특정 상품을 앞으로 제외하라는 요청에만 사용.')


class ProductSearch(Strict):
    queries: list[str] = Field(min_length=1, max_length=2, description='상품 종류와 용도. 예: ["우산"]. 사람이나 기념일만 쓰지 않음.')
    changes: ProductFilterChanges = Field(default_factory=ProductFilterChanges, description='이번 요청에서 바꾼 조건만. 생략한 조건은 서버가 유지.')


def public_filters(filters):
    return {key: value for key, value in filters.items() if key not in ('recipient_scope', 'product_type')}


class SimpleHarness(ConsultHarness):
    variant = 'stable-v4.8'
    context_model = ProductSearch

    def context_tool(self, s):
        tool = sparse_tool('gift_context', self.context_model,
            '실제 선물 후보 조회. queries에 상품 종류·용도를, changes에 바꾼 가격·브랜드·카테고리·제외 상품 조건만 보낸다. 생략한 조건은 유지한다.')
        fields = tool['function']['parameters']['properties']['changes']['properties']
        for key in ('category_ids', 'exclude_category_ids'):
            fields[key]['items']['enum'] = sorted(self.catalog.category_ids)
        return tool

    def parse_context(self, payload, schema, s, request):
        # Validate before the legacy adapter: removed controls are unsupported,
        # never silently ignored or allowed through its broader internal type.
        args = self.context_model.model_validate(payload)
        return super().parse_context(args.model_dump(exclude_unset=True), schema, s, request)

    async def prepare_turn(self, s, request):
        # Migrate retired controls from old sessions once, independently of user
        # wording. Preserve price/brand/category conditions and displayed facts.
        if s.filters.recipient_scope != 'current' or s.filters.product_type is not None:
            s.filters = s.filters.model_copy(update={'recipient_scope': 'current', 'product_type': None})
            s.last_search = {}
        await super().prepare_turn(s, request)

    def system_prompt(self, s, request):
        prompt = super().system_prompt(s, request)
        prompt = prompt.replace('가격·배송 제한의 null은 삭제', '가격 제한의 null은 삭제')
        prompt = prompt.replace(' 상품 종류와 배송 방식을 혼동하지 않는다.', '')
        return prompt.replace(
            '다른 수신자로 명시적으로 바꾸면 recipient_scope=other, 원래 수신자로 돌아오면 current다. 이때 서버가 이전 조건을 초기화한다.',
            '수신자 정보는 앱에서 선택된 사람을 기준으로 제공된다. 사용자가 검색 조건 초기화를 요청하면 해당 가격은 null, 목록은 []로 명시적으로 지운다.')

    def state(self, s, request, context):
        state = copy.deepcopy(super().state(s, request, context))
        state['filters'] = public_filters(state['filters'])
        for key in ('last_search', 'gift_context'):
            if state.get(key) and 'filters' in state[key]:
                state[key]['filters'] = public_filters(state[key]['filters'])
        return state

    def context_observed(self, s, context, filters):
        # Retain full internal filters for cache equality/validation, but expose
        # only supported filters in the tool response and future model history.
        super().context_observed(s, context, filters)
        context['filters'] = public_filters(context['filters'])

    def inspect_result(self, s):
        result = super().inspect_result(s)
        result['filters'] = public_filters(result['filters'])
        return result
