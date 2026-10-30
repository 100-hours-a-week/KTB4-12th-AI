"""Model prose passes through; catalog prices remain authoritative. No provider calls."""
from types import SimpleNamespace

import pytest

import test_low_latency as fixture
from app.gift_adapter import CatalogAdapter, view
from app.gift_core.contracts import Filters, Profile
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
def test_numeric_prose_is_not_judged_after_generation(reason):
    cards = validate(reason)
    assert len(cards) == 1
    assert cards[0]['reason'] == reason


@pytest.mark.parametrize('filters', [Filters(), Filters(max_price=60000), Filters(min_price=50000)])
def test_old_budget_prose_is_not_reinterpreted(filters):
    cards = validate('제품이 44,000원으로 5만원 이하입니다.', filters=filters)
    assert len(cards) == 1


@pytest.mark.parametrize('filters', [Filters(), Filters(max_price=60000), Filters(min_price=50000)])
def test_equal_numbers_are_not_interpreted_as_budget_claims(filters):
    cards = validate('요청한 상한 5만원 이하입니다.', price=50000, filters=filters)
    assert len(cards) == 1


def test_comparison_direction_is_model_prose():
    cards = validate('제품이 44,000원으로 5만원 이상입니다.', description='구성: 2개 1세트. 예전 가격 50,000원.')
    assert len(cards) == 1


def test_no_selection_budget_recheck():
    cards = validate('제품이 54,000원으로 5만원 이하입니다.', price=54000)
    assert len(cards) == 1


def test_actual_catalog_price_remains_unchanged():
    cards = validate('상품 가격은 54,000원입니다.', price=54000)
    assert len(cards) == 1 and cards[0]['price'] == 54000


def test_quantity_words_are_not_semantically_judged():
    assert validate('양말 3켤레 선물입니다.', description='양말 3pack 선물상자.')[0]['reason'] == '양말 3켤레 선물입니다.'


def test_one_card_can_describe_two_pieces_without_becoming_two_recommendations():
    assert len(validate('등산스틱 2개 1세트로 야외에서 쓰기 좋아요.')) == 1
