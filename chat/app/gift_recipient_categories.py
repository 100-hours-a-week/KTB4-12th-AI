"""v4.9: category exclusions belong to recipient settings, not model decisions."""
import copy

from pydantic import Field

from app.gift_core.contracts import Strict
from app.gift_guided import sparse_tool
from app.gift_simple import SimpleHarness


class SearchChanges(Strict):
    min_price: int | None = Field(default=None, ge=0, le=1_000_000_000, description='변경할 최저가. null은 삭제. 생략은 유지.')
    max_price: int | None = Field(default=None, ge=0, le=1_000_000_000, description='변경할 최고가. null은 삭제. 생략은 유지.')
    brands: list[str] = Field(default_factory=list, max_length=20, description='포함 브랜드 목록 교체. []는 제한 삭제. 생략은 유지.')
    exclude_brands: list[str] = Field(default_factory=list, max_length=20, description='제외 브랜드 목록 교체. []는 제한 삭제. 생략은 유지.')
    exclude_product_ids: list[str] = Field(default_factory=list, max_length=30, description='특정 상품을 앞으로 제외하라는 요청에만 사용. []는 삭제. 생략은 유지.')


class ProductSearch(Strict):
    queries: list[str] = Field(min_length=1, max_length=2, description='찾을 상품 종류·용도. 예: ["우산"]. 제외 조건을 검색어에 넣어 필터처럼 사용하지 않는다.')
    changes: SearchChanges = Field(default_factory=SearchChanges, description='이번 요청에서 바뀐 가격·브랜드·개별 상품 제외만. 생략한 조건은 유지.')


RETIRED_CATEGORIES = ('category_ids', 'exclude_category_ids')


def model_filters(filters):
    return {k: copy.deepcopy(v) for k, v in filters.items() if k not in RETIRED_CATEGORIES}


class RecipientCategoryHarness(SimpleHarness):
    variant = 'stable-v4.9'
    context_model = ProductSearch

    def context_tool(self, s):
        return sparse_tool('gift_context', self.context_model,
            '선물 후보 조회. queries에 상품 종류·용도, changes에 바뀐 가격·브랜드·개별 상품 제외를 보낸다. 수신자 비선호 카테고리는 서버가 자동 제외하며 이 도구로 변경하지 않는다.')

    async def prepare_turn(self, s, request):
        # Retire old user category constraints independently of request wording.
        # Resolved dislikes are recalculated from the current profile on search;
        # never confuse them with user-origin filters or carry a removed dislike.
        public_exclusions = getattr(s, 'public_exclusions', [])
        if s.filters.category_ids or public_exclusions:
            s.last_search = {}
        s.filters = s.filters.model_copy(update={
            'category_ids': [],
            'exclude_category_ids': self.catalog.expand(s.profile.disliked_category_ids),
        })
        s.public_exclusions = []
        if hasattr(s, 'filter_sources'):
            s.filter_sources = model_filters(s.filter_sources)
        # A reused adapter must not carry an old public category patch.
        if hasattr(self.catalog, 'public_filters'):
            self.catalog.public_filters = self.catalog.public_filters.model_copy(update={
                'category_ids': [], 'exclude_category_ids': []})
        await super().prepare_turn(s, request)

    def system_prompt(self, s, request):
        prompt = super().system_prompt(s, request)
        prompt = prompt.replace(
            'category_ids·exclude_category_ids·exclude_product_ids는 명시한 종류/제외 조건에만 쓴다.',
            'exclude_product_ids는 명시한 개별 상품 제외 조건에만 쓴다.')
        return prompt.replace('STATE_JSON\n', '''상품 종류는 queries로 검색한다. 카테고리 ID 목록을 만들거나 검색 조건에 보내지 않는다.
수신자가 설정한 비선호 카테고리는 서버가 모든 검색 경로에서 자동 제외한다. 검색 조건 전체 초기화는 가격·브랜드·개별 상품 제외만 지우며 수신자 설정은 유지된다. 비선호 카테고리를 추가·해제하려면 앱의 수신자 설정에서 바꿔야 한다고 안내한다. 대화나 검색어로 설정을 바꿨다고 말하지 않는다. 부정 검색어로 카테고리 제외가 보장된다고 약속하지 않는다.
과거 도구 기록은 당시 실행의 증거이며 현재 제공된 도구 스키마가 우선한다. 과거 카테고리 필터는 현재 검색 조건에 이어지지 않는다.
STATE_JSON\n''')

    def state(self, s, request, context):
        state = super().state(s, request, context)
        state.pop('categories', None)
        state['filters'] = model_filters(state['filters'])
        for key in ('last_search', 'gift_context'):
            if state.get(key) and 'filters' in state[key]:
                state[key]['filters'] = model_filters(state[key]['filters'])
        state['category_policy'] = 'recipient_settings; enforced_by_server; not_editable_by_tool'
        return state

    def context_observed(self, s, context, filters):
        # Keep complete internal filters for cache equality, expose only the
        # supported public fields. Recipient profile is already part of scopeKey.
        super().context_observed(s, context, filters)
        context['filters'] = model_filters(context['filters'])

    def inspect_result(self, s):
        result = super().inspect_result(s)
        result['filters'] = model_filters(result['filters'])
        return result
