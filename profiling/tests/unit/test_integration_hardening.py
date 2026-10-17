"""합성 통합 회귀: 채택한 수치·API 보완과 develop에서 보존한 Profiler 정책을 구분한다."""

from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from profiling import pipeline, selection
from profiling.embedding import normalize
from profiling.pipeline import DislikeMode, PoolStrategy
from profiling.schemas import ProductRecord
from profiling.types import ProfileRequest


def _p(pid, *, brand="fixture", name="합성 상품", views=1):
    return ProductRecord(
        productId=pid,
        name=name,
        brand=brand,
        description=None,
        categoryId=1,
        categoryName="합성",
        parentCategoryId=10,
        parentCategoryName="합성 대분류",
        productKind="합성",
        price=1000,
        availability="available",
        updatedAt=datetime(2026, 10, 16, tzinfo=UTC),
        viewCount=views,
    )


@pytest.mark.parametrize(
    ("tag", "name"),
    [("LG", "Allgood cup"), ("MAC", "machine toy"), ("SONY", "Sonya doll")],
)
def test_ascii_disliked_terms_do_not_match_inside_unrelated_words(tag, name):
    assert not selection.disliked_hit(
        _p(1, brand="synthetic", name=name), selection.Tag("브랜드", tag)
    )


@pytest.mark.parametrize('strategy', list(PoolStrategy))
def test_uninterpretable_dislike_keeps_develop_matcher_policy(strategy):
    request = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference='합성',
                             disliked_categories=[], reviews=[])
    result, _ = pipeline.make_pool(request, [_p(1)], 1, uuid4(), pool_strategy=strategy,
                                   disliked_tags=[selection.Tag('브랜드', 'x')])
    assert result.product_ids == [1]


def test_unknown_explicit_category_fails_before_tagger_or_profile_write():
    from tests.unit.test_pipeline import FakeCatalog, FakeStore, FakeRecipientStore
    from types import SimpleNamespace
    from profiling.types import DislikedCategory, RunStatus
    request = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference='합성',
        disliked_categories=[DislikedCategory(category_id=999, category_name='매핑 없는 합성 분류')], reviews=[])
    calls = []
    tagger = SimpleNamespace(model='synthetic', extract=lambda *a: (calls.append(a), '{"tags":[]}')[1])
    store, profiles = FakeStore(), FakeRecipientStore()
    catalog = FakeCatalog([_p(1)])
    def reject_unknown(categories):
        assert categories == request.disliked_categories
        raise ValueError('UNKNOWN_EXPLICIT_DISLIKE_CATEGORY')
    catalog.validate_disliked_categories = reject_unknown
    result = pipeline.profile(request, catalog=catalog, store=store,
                              recipient_store=profiles, tagger=tagger)
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == 'ValueError'  # The boundary redacts private error text.
    assert not calls and not profiles.rows


@pytest.mark.parametrize('tag, brand', [('ZETA', 'ZETACARE'), ('ROSE', 'ROSEDALE')])
def test_unknown_ascii_brand_keeps_develop_substring_policy(tag, brand):
    assert selection.disliked_hit(_p(1, brand=brand), selection.Tag('브랜드', tag))


@pytest.mark.parametrize('text, expected', [('죽', None), ('꿀', '벌꿀 허니')])
def test_short_preference_keeps_develop_expansion_policy(text, expected):
    from profiling import vocab
    assert vocab.query_text('카테고리', text) == expected


@pytest.mark.parametrize("brand,expected", [("LG", True), ("LG전자", True), ("[LG] official", False), ("L-G", True)])
def test_ascii_brand_terms_keep_develop_field_matching(brand, expected):
    assert selection.disliked_hit(_p(1, brand=brand), selection.Tag("브랜드", "LG")) is expected


@pytest.mark.parametrize("strategy", list(PoolStrategy))
@pytest.mark.parametrize("mode", list(DislikeMode))
def test_disliked_tags_apply_to_all_pool_strategies_and_fill_modes(strategy, mode):
    products = [_p(1, brand="소니", views=900), _p(2, brand="fixture", views=1)]
    request = ProfileRequest(
        recipient_user_id=1,
        source_version=1,
        gift_preference="합성",
        disliked_categories=[],
        reviews=[],
    )
    result, rule = pipeline.make_pool(
        request,
        products,
        3,
        uuid4(),
        pool_strategy=strategy,
        dislike_mode=mode,
        disliked_tags=[selection.Tag("브랜드", "소니")],
    )
    assert result.product_ids == [2]
    if strategy is PoolStrategy.S0:
        assert rule == (pipeline.POOL_RULE_V2 if mode is DislikeMode.EXCLUDE else pipeline.POOL_RULE_V2_PENALTY)
    else:
        assert "+disliked" in rule


@pytest.mark.parametrize("magnitude", [1e-300, 1e-30, 1e30, 1e300])
def test_normalize_extreme_nonzero_vectors(magnitude):
    raw = np.zeros((2, 768), dtype=np.float64)
    raw[:, 0] = magnitude
    raw[:, 1] = -magnitude
    result = normalize(raw)
    assert result.dtype == np.float32 and np.isfinite(result).all()
    assert np.allclose(np.linalg.norm(result.astype(np.float64), axis=1), 1)
    assert np.allclose(result[:, :2], [2**-0.5, -(2**-0.5)])


def test_normalize_preserves_ordinary_float32_values_exactly():
    raw = np.random.default_rng(817).normal(size=(9, 768)).astype(np.float32)
    assert np.array_equal(
        normalize(raw), raw / np.linalg.norm(raw, axis=1, keepdims=True)
    )


@pytest.mark.parametrize(
    "raw",
    [
        np.zeros((1, 768)),
        np.full((1, 768), np.nan),
        np.full((1, 768), np.inf),
        np.ones((768,)),
        np.ones((1, 768), dtype=bool),
        np.array([["private text"]]),
    ],
)
def test_normalize_rejects_invalid_vectors_without_echoing_values(raw):
    with pytest.raises(ValueError, match="^Invalid embedding matrix$"):
        normalize(raw)


def test_native_similarity_failure_keeps_develop_rank_fallback():
    class Embedding:
        def encode(self, texts):
            return np.eye(768)[: len(texts)]

        def similarities(self, ids, queries):
            raise RuntimeError("synthetic similarity outage")

    ranked = pipeline.rank_products(
        ProfileRequest(
            recipient_user_id=1,
            source_version=1,
            gift_preference="합성",
            disliked_categories=[],
            reviews=[],
        ),
        [_p(1)],
    )
    result = selection.select_pool(ranked, 1, allow_disliked=False,
                                   tags=[selection.Tag("카테고리", "합성")], embedding=Embedding())
    assert result.product_ids == [1] and result.rule == selection.POOL_RULE_D_EMBED_FAILED


@pytest.mark.parametrize(
    ("spec_name", "review", "expected_calls"),
    [
        ("tag3disliked", "소니 제품은 싫어요", 0),
        ("tag3", "소니 제품은 싫어요", 0),
        ("tag3disliked", None, 0),
        ("tag3disliked", "ㅋㅋㅋㅋ", 0),
    ],
)
def test_low_rating_text_keeps_develop_zero_call_policy(
    spec_name, review, expected_calls
):
    import json
    from types import SimpleNamespace

    from profiling import tagging
    from profiling.types import Review

    calls = []

    def extract(*args):
        calls.append(True)
        return json.dumps(
            {
                "tags": [
                    {"axis": "브랜드", "tag": "소니", "ref": "r1", "polarity": "비선호"}
                ]
            }
        )

    request = ProfileRequest(
        recipient_user_id=1,
        source_version=1,
        gift_preference=None,
        disliked_categories=[],
        reviews=[Review(product_id=1, rating=1, review_text=review)],
    )
    run = pipeline._extract_tags(
        request,
        [_p(1)],
        tagger=SimpleNamespace(model="synthetic", extract=extract),
        disliked_names=[],
        spec=tagging.load_spec(spec_name),
    )
    assert len(calls) == expected_calls
    assert [t.tag for t in run.validation.dislikes] == (
        ["소니"] if expected_calls else []
    )
    assert run.validation.preferred_tags == []


@pytest.mark.parametrize('where', ['preference', 'review'])
def test_g5_truncation_keeps_develop_mask_then_prefix_policy(where):
    from tests.unit.test_pipeline import FakeCatalog, FakeStore, FakeRecipientStore
    from profiling.types import RunStatus, Review
    from types import SimpleNamespace
    text = '합성 캠핑 선물을 좋아해요. ' * 100 + '하지만 소니 제품은 절대 싫어요.'
    calls = []
    tagger = SimpleNamespace(model='synthetic', extract=lambda *a: (calls.append(a), '{"tags":[]}')[1])
    rq = ProfileRequest(recipient_user_id=1, source_version=8,
        gift_preference=text if where=='preference' else None, disliked_categories=[],
        reviews=[Review(product_id=1,rating=1,review_text=text)] if where=='review' else [])
    store=FakeStore();profiles=FakeRecipientStore()
    result=pipeline.profile(rq,catalog=FakeCatalog([_p(1)]),store=store,recipient_store=profiles,tagger=tagger)
    assert result.status is RunStatus.RESULT_READY and result.failure_reason is None
    assert len(calls) == (1 if where == 'preference' else 0)
    if calls:
        assert '하지만 소니 제품은 절대 싫어요.' not in calls[0][1]
    profile = profiles.get(1)
    retained = profile.gift_preference_clean if where == 'preference' else profile.reviews[0].text_clean
    assert len(retained) == 1000 and '하지만 소니 제품은 절대 싫어요.' not in retained


def test_private_exception_first_line_and_debug_trace_never_enter_logs(caplog):
    from tests.unit.test_pipeline import FakeCatalog, FakeStore, FakeRecipientStore, _rq
    from profiling.types import RunStatus
    marker='SYNTHETIC_PRIVATE_PAYLOAD_97531'
    class Broken(FakeRecipientStore):
        def upsert(self, profile):raise RuntimeError(marker+'\nDETAIL: '+marker)
    with caplog.at_level('DEBUG', logger='profiling.pipeline'):
        result=pipeline.profile(_rq(),catalog=FakeCatalog([_p(1)]),store=FakeStore(),recipient_store=Broken())
    assert result.status is RunStatus.FAILED
    assert marker not in result.failure_reason and marker not in caplog.text
    assert all(not record.exc_info for record in caplog.records if record.name=='profiling.pipeline')
