"""v4.11: small native decisions and lossless, non-redundant model history.

Only the provider view is projected. Full evidence and actual call arguments
remain in the journal; product choice belongs to the model.
"""
import json
from typing import Literal

from pydantic import Field

from app.gift_browse import BrowseHarness
from app.gift_compact import CompactHarness, SYSTEM as COMPACT_SYSTEM, product_input
from app.gift_core.contracts import Strict
from app.gift_guided import sparse_tool


class Selection(Strict):
    product_ids: list[str] = Field(max_length=3, description='적합한 후보 ID를 표시 순서대로. 없으면 [].')
    message: str = Field(default='', max_length=300,
        description='선택 이유·미확인 사항 등 필요한 안내만. 단순 완료 인사나 카드 내용 반복이면 생략.')


class Listing(Strict):
    product_ids: list[str] = Field(max_length=30, description='표시할 상품 ID를 순서대로. 없으면 [].')
    format: Literal['names', 'brief'] = Field(default='names', description='names(기본): 이름만. brief: 사진·가격·설명.')
    message: str = Field(default='', max_length=300, description='brief에서 필요한 안내만. 카드 내용 반복이면 생략.')

    @property
    def one_per_brand(self):
        # Legacy renderer interface only; never a schema field or grouping rule.
        return False


class Inspection(Strict):
    pass


SYSTEM = COMPACT_SYSTEM.replace(
    '상품을 새로 추천하거나 다시 표시할 때는 gift_context를 한 번 호출한다. queries는 상품 종류·용도 한두 개이며,',
    '새 상품 추천·조건 변경·나머지 후보 선택은 gift_context를 한 번 호출한다. 직전 표시 상품의 이름·사진만 다시 보여 달라면 제공된 list_gifts로 바로 표시한다. queries는 찾을 종류·용도 하나가 기본이며 서로 다른 두 종류일 때만 두 개를 쓴다. 동의어를 중복 조회하지 않는다.')
SYSTEM = SYSTEM.replace(
    '브랜드별 하나씩 요청이면 one_per_brand=true와 각 브랜드의 적합한 ID를 사용한다. message에는 짧은 안내만 쓰고 상품 사실을 반복하지 않는다.',
    '브랜드별 하나씩 요청이면 직접 서로 다른 브랜드의 적합한 ID를 고른다. message는 선택 이유·미확인 사항처럼 필요한 내용이 있을 때만 쓴다. 완료 인사·검색어·적용 조건·상품명·가격을 반복하지 않는다. 상품 카드나 이름 목록만으로 답이 되면 message를 생략한다.')
SYSTEM = SYSTEM.replace('나머지 요청은 직전 표시 ID를 제외해 선택하고,', '나머지 요청은 gift_context 후 직전 표시 ID를 제외해 선택하고,')


class CleanIOHarness(CompactHarness):
    variant = 'stable-v4.11'
    history_versions = ('stable-v4.9', 'stable-v4.10', 'stable-v4.11')
    selection_model = Selection
    listing_model = Listing
    inspection_model = Inspection

    def system_prompt(self, s, request):
        return SYSTEM

    def selection_tool(self, s, eligible):
        # Candidate identities already occur in the tool result. Validate them
        # against observed rows on execution, not repeated enums in two schemas.
        return sparse_tool('show_gifts', self.selection_model, '조회한 후보로 추천 카드 최대 3개를 표시한다.')

    def list_tool(self, eligible):
        return sparse_tool('list_gifts', self.listing_model, '조회한 후보를 이름 또는 사진 목록으로 표시한다.')

    def inspect_tool(self):
        return sparse_tool('inspect_search', self.inspection_model, '최근 조회의 조건·후보 수·브랜드 현황을 답한다.')

    def context_tool(self, s):
        tool = super().context_tool(s)
        fields = tool['function']['parameters']['properties']
        fields['queries']['description'] = '찾을 상품 종류·용도. 한 종류는 검색어 하나. 부정 표현은 제외 필터가 아니다.'
        fields['changes']['description'] = '바뀐 조건만. 생략=유지, 가격 null·목록 []=해제. 가격은 원.'
        descriptions = {'min_price':'최저가', 'max_price':'최고가', 'brands':'포함 브랜드',
                        'exclude_brands':'제외 브랜드', 'exclude_product_ids':'앞으로 제외할 개별 상품 ID'}
        for key, description in descriptions.items():
            fields['changes']['properties'][key]['description'] = description
        return tool

    def answer_tools(self, s):
        tools = super().answer_tools(s)
        cached = self.cached_search(s)
        displayed_ids = {p['product_id'] for p in self.displayed(s)}
        can_repeat = cached and displayed_ids.intersection(p['product_id'] for p in cached['products'])
        if 'search' not in (self._pending.get() or {}) and can_repeat:
            tool = self.list_tool(())
            tool['function']['description'] = '직전 표시 상품만 이름·사진으로 다시 표시한다. 새 상품·조건 변경은 gift_context를 먼저 호출한다.'
            tools.append(tool)
        return tools

    def validate_list(self, args, s):
        cached, by_id = BrowseHarness.validate_list(self, args, s)
        pending = self._pending.get()
        if 'search' not in (pending or {}) and not set(args.product_ids) <= {p['product_id'] for p in self.displayed(s)}:
            raise ValueError('search_required_for_new_products')
        if pending is not None:
            pending['display_search'] = cached
        return cached, by_id

    def feedback(self, exc):
        if str(exc) == 'search_required_for_new_products':
            return {'code': str(exc), 'hint':'직전 표시 이외의 상품은 gift_context로 먼저 조회하세요.'}
        return super().feedback(exc)

    def selection_contract(self):
        return {'available_operations':['show_gifts', 'list_gifts', 'inspect_search'],
                'instruction':'추천 카드 최대 3개는 show_gifts, 이름·사진 목록은 list_gifts, 검색 현황만 물으면 inspect_search. 적합한 ID만 선택하고 필요한 안내만 message에 쓰세요.'}

    def show_args(self, selection, filters):
        args = super().show_args(selection, filters)
        args.message = selection.message
        return args

    def render_list(self, args, by_id):
        if args.product_ids and args.format == 'brief':
            return args.message
        return BrowseHarness.render_list(self, args, by_id)

    def answer_result(self, name, payload, text, s):
        result = super().answer_result(name, payload, text, s)
        if name == 'list_gifts':
            # Retain historical facts after a subsequent search replaces state.
            by_id = {p['product_id']: p for p in self.cached_search(s)['products']}
            result['products'] = [product_input(by_id[i]) for i in payload['product_ids']]
        return result

    def prompt_history(self, s):
        messages = super().prompt_history(s)  # Already deep-copied by StableV4.
        known = {p['product_id']: product_input(p) for p in [*s.last_products, *self.displayed(s)]}
        calls = {}
        for index, message in enumerate(messages):
            for call in message.get('tool_calls', []):
                calls[call['id']] = call['function']['name']
            if message['role'] != 'tool':
                continue
            name = calls.get(message.get('tool_call_id'))
            result = json.loads(message['content'])
            if 'error' in result:
                continue
            if name == 'gift_context':
                # Keep prior effective conditions and bounded search count.
                projected = {k:result[k] for k in ('queries','filters','retrieved_count') if k in result}
            elif name in ('show_gifts', 'list_gifts', 'recall_gift', 'inspect_search'):
                projected = {'status': result.get('status', 'completed')}
                facts = [product_input(row) for row in result.get('products', [])]
                facts = [p for p in facts if any(known.get(p['product_id'], {}).get(k) != v for k,v in p.items())]
                if facts:
                    projected['products'] = facts
                following = []
                for later in messages[index+1:]:
                    if later['role'] == 'user':
                        break
                    if later['role'] == 'assistant' and later.get('content'):
                        following.append(later['content'])
                if result.get('text') and result['text'] not in following:
                    projected['text'] = result['text']
            else:
                continue
            message['content'] = json.dumps(projected, ensure_ascii=False)
        return messages
