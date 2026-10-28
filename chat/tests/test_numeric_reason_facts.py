"""Grounded money/composition claims; no provider calls or private fixtures."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import test_low_latency as fixture
from app.gift_adapter import CatalogAdapter, view
from app.gift_core.contracts import Filters, Profile
from app.gift_core.provider import ProviderError
from app.integrated_harness import IntegratedHarness


def validate(reason, *, price=44000, description='구성: 등산스틱 2개 1세트.', filters=None):
    harness = object.__new__(IntegratedHarness)
    harness.catalog = CatalogAdapter(None, fixture.META, {})
    row = view({**fixture.PRODUCT, 'name': '합성 야외용품', 'price': price,
                'description': description})
    harness.catalog.products = {'101': row}
    filters = filters if filters is not None else Filters(max_price=50000)
    payload = {'product_ids': ['101'], 'reasons': [{'product_id': '101', 'reason': reason}]}
    args = harness.show_args(harness.selection_model.model_validate(payload), filters)
    return harness.validate_show(args, SimpleNamespace(filters=filters, profile=Profile()), {'101': row})[0]


@pytest.mark.parametrize('reason', [
    '44,000원으로 5만원 이하입니다.',
    '44,000 원으로 5 만 원 이내입니다.',
    '상품이 4.4만원으로 50,000원 이하입니다.',
    '용도에 맞고, 야외용 제품이 44,000원으로 5만원 이하입니다.',
    '야외에서 사용할 제품이 44,000원으로 5만원 이하입니다.',
    '상품 가격은 44,000원입니다.',
    '등산스틱 2개 1세트로 야외에서 쓰기 좋고 44,000원입니다.',
    '등산스틱 2 개 1 세트로 야외에서 쓰기 좋고 4.4만 원입니다.',
    '등산스틱 2개1세트로 야외에서 쓰기 좋고 44,000원입니다.',
])
def test_grounded_budget_money_and_adjacent_composition_counts(reason):
    assert validate(reason)[0]['reason'] == reason


@pytest.mark.parametrize('reason', [
    '상품 가격은 50,000원입니다.',
    '제품이 45,000원으로 5만원 이하입니다.',
    '제품이 44,000원으로 5만원 이상입니다.',
    '배송비도 44,000원으로 5만원 이하입니다.',
    '구독료가 44,000원으로 5만원 이하입니다.',
    '제품이 44,000원으로 5만원 이하가 아닙니다.',
    '제품이 44,000원으로 5만원 이하이고 무게는 5만g입니다.',
    '상품 3개 1세트라 야외에서 쓰기 좋아요.',
    '상품 2개 2세트라 야외에서 쓰기 좋아요.',
    '상품은 2kg이고 1m입니다.',
    '상품 가격은 -44,000원입니다.',
    '상품 가격은 − 44,000원입니다.',
    '상품 가격은 44,000달러입니다.',
    '상품 가격은 4.4.0만원입니다.',
])
def test_budget_or_composition_never_authorizes_invented_facts(reason):
    with pytest.raises(ProviderError, match='unsupported_numeric_claim'):
        validate(reason)


@pytest.mark.parametrize('filters', [Filters(), Filters(max_price=60000), Filters(min_price=50000)])
def test_removed_changed_or_reversed_budget_does_not_reuse_old_boundary(filters):
    with pytest.raises(ProviderError, match='unsupported_numeric_claim'):
        validate('제품이 44,000원으로 5만원 이하입니다.', filters=filters)


@pytest.mark.parametrize('filters', [Filters(), Filters(max_price=60000), Filters(min_price=50000)])
def test_stale_budget_is_rejected_even_when_its_amount_equals_product_price(filters):
    with pytest.raises(ProviderError, match='unsupported_numeric_claim'):
        validate('요청한 상한 5만원 이하입니다.', price=50000, filters=filters)


def test_inverted_comparison_is_rejected_even_when_both_amounts_are_in_catalog():
    with pytest.raises(ProviderError, match='unsupported_numeric_claim'):
        validate('제품이 44,000원으로 5만원 이상입니다.',
                 description='구성: 2개 1세트. 예전 가격 50,000원.')


def test_below_budget_claim_cannot_authorize_over_budget_product():
    with pytest.raises((ValueError, ProviderError)):
        validate('제품이 54,000원으로 5만원 이하입니다.', price=54000)


def test_correct_numeric_fact_cannot_authorize_over_budget_selection():
    with pytest.raises(ValueError, match='selected_product_violates_filters'):
        validate('상품 가격은 54,000원입니다.', price=54000)


def test_pack_is_not_an_unsupported_pairs_conversion():
    with pytest.raises(ProviderError, match='unsupported_numeric_claim'):
        validate('양말 3켤레 선물입니다.', description='양말 3pack 선물상자.')
    assert validate('양말 3켤레 선물입니다.', description='구성: 양말 3켤레.')


def test_one_card_can_describe_two_pieces_without_becoming_two_recommendations():
    cards = validate('등산스틱 2개 1세트로 야외에서 쓰기 좋아요.')
    assert len(cards) == 1
    harness = object.__new__(IntegratedHarness)
    with pytest.raises(ValidationError):
        harness.selection_model.model_validate({
            'product_ids': ['1', '2', '3', '4'],
            'reasons': [{'product_id': str(n), 'reason': '구성: 1개'} for n in range(1, 5)],
        })
