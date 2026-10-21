"""pipeline — 가짜 adapter(ports 모양)로 v2 흐름(별점 편향 · 태그 · 30개 고르기 D · 저장 · 콜백 본문). DB·HTTP 없음."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from profiling import pipeline, selection
from profiling.ports import NoActiveCatalog, TagExtractError
from profiling.schemas import ProductRecord, ProfileExtractRequest
from profiling.types import (
    CategoryScore,
    DislikedCategory,
    ErrorCode,
    ProfileRequest,
    Review,
    RunStatus,
    preference_hash,
)

CV = UUID(int=7)   # 시험용 카탈로그 버전 ID

# ---------------------------------------------------------------- 가짜


def _product(pid: int, cat_id: int, cat_name: str, availability: str = "available", views: int = 0,
             parent_id: int | None = None, parent_name: str | None = None) -> ProductRecord:
    return ProductRecord(productId=pid, name=f"p{pid}", brand="b", description=None, categoryId=cat_id, categoryName=cat_name,
                         parentCategoryId=parent_id, parentCategoryName=parent_name,
                         price=1000, availability=availability, updatedAt=datetime(2026, 9, 21, tzinfo=UTC), viewCount=views)


class FakeCatalog:
    def __init__(self, products, version_id: UUID = CV, fail: bool = False):
        self.products, self.version_id, self.fail = products, version_id, fail

    def active(self):
        if self.fail:
            raise NoActiveCatalog("없음")
        return self.version_id, self.products

    def by_id(self, product_id):
        return next((p for p in self.products if p.productId == product_id), None)


class FakeStore:
    def __init__(self):
        self.saved = {}
        self.history = []                      # save() 순서 — RUNNING → RESULT_READY 를 확인하기 위해

    def save(self, outcome):
        self.saved[outcome.recipient_user_id] = outcome
        self.history.append(outcome.status)

    def get(self, rid):
        return self.saved.get(rid)


class FakeRecipientStore:
    def __init__(self, fail=False):
        self.rows, self.fail = {}, fail

    def upsert(self, profile):
        if self.fail:
            raise RuntimeError("profiles down")
        self.rows[profile.recipient_user_id] = profile

    def get(self, rid):
        return self.rows.get(rid)

    def delete(self, rid):
        return self.rows.pop(rid, None) is not None


# 카테고리 3종 × 재고 있음/없음 섞어 40개: 100번대=뷰티, 200번대=주방, 300번대=완구
PRODUCTS = [_product(pid, cat, name, availability="unavailable" if pid % 10 == 0 else "available")
            for pid, (cat, name) in enumerate(((c, n) for c, n in [(100, "뷰티"), (200, "주방"), (300, "완구")] for _ in range(14)), start=1)][:40]


def _rq(disliked=(), pref=None, reviews=()) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=9073, source_version=3, gift_preference=pref,
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked],
                          reviews=list(reviews))


# ---------------------------------------------------------------- to_internal · needs_model


def test_to_internal_converts_nested_items() -> None:
    body = ProfileExtractRequest(recipientUserId=1, sourceVersion=2, dislikedCategories=[{"categoryId": 701, "categoryName": "도서·음반"}],
                                 giftPreference=None, reviews=[{"productId": 10, "rating": 5, "reviewText": None}])
    rq = pipeline.to_internal(body)
    assert rq.recipient_user_id == 1 and rq.source_version == 2 and rq.gift_preference is None
    assert rq.disliked_categories[0].category_name == "도서·음반" and rq.reviews[0].product_id == 10


@pytest.mark.parametrize("pref,reviews,expected", [
    (None, (), False),                                             # v1
    ("휴대용 좋아요", (), True),                                     # 취향만
    (None, (Review(product_id=1, rating=5),), True),               # 리뷰만 (취향 null인 v3 수신자)
])
def test_needs_model(pref, reviews, expected) -> None:
    assert pipeline.needs_model(_rq(pref=pref, reviews=reviews)) is expected


# ---------------------------------------------------------------- has_no_input · 입력 없음 → 추천 0개 (10-09 결정 C)


@pytest.mark.parametrize("pref,disliked,reviews,expected", [
    (None, (), (), True),                                            # 전부 빈 값
    ("  ", (), (), True),                                            # 공백만인 취향 문장도 "없음"
    (None, ((1, "뷰티"),), (), False),                               # 비선호만 있어도 입력 있음
    ("휴대용", (), (), False),
    (None, (), (Review(product_id=1, rating=3),), False),
])
def test_has_no_input(pref, disliked, reviews, expected) -> None:
    assert pipeline.has_no_input(_rq(disliked=disliked, pref=pref, reviews=reviews)) is expected


def test_profile_all_empty_input_sends_zero_recommendations() -> None:
    """기존 사용자가 비선호·취향·리뷰를 전부 지우면 BE 가 빈 요청을 보낸다 → AI 는 추천 0개(빈 배열 콜백), 사본은 빈 값으로 덮인다.
    BE 는 0행을 저장하고 상품 목록을 인기순으로 대체한다(10-09 결정 C). 인기순 30개를 'AI 추천'으로 내보내지 않는다."""
    store, rstore = FakeStore(), FakeRecipientStore()
    out = pipeline.profile(_rq(), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=rstore)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == [] and out.search.catalog_version_id == CV
    assert store.history == [RunStatus.RUNNING, RunStatus.RESULT_READY]          # 콜백 본문(빈 배열)까지 기록됨
    assert rstore.get(9073).disliked_categories == [] and rstore.get(9073).recommended_product_ids == []


# ---------------------------------------------------------------- build_pool


def test_pool_excludes_disliked_and_unavailable_keeps_order() -> None:
    rq = _rq(disliked=[(200, "주방")])
    res = pipeline.build_pool(rq, PRODUCTS, pool_size=30, catalog_version_id=CV)
    by_id = {p.productId: p for p in PRODUCTS}
    assert res.catalog_version_id == CV and res.query_text == ""
    assert all(by_id[i].categoryId != 200 and by_id[i].availability != "unavailable" for i in res.product_ids)
    assert res.product_ids == sorted(res.product_ids)                          # 조회수가 전부 0이면 카탈로그 순서 (동점 규칙)
    assert len(res.product_ids) == min(30, sum(1 for p in PRODUCTS if p.availability != "unavailable" and p.categoryId != 200))


def test_pool_keeps_unknown_availability() -> None:
    """재고를 모르는 상품(unknown)은 풀에 남는다 — 09-23 합의. 실제 재고 판단은 Backend 몫이고,
    패키지로 적재한 카탈로그는 전건 unknown이라 이 규칙이 아니면 풀이 0건이 된다."""
    products = [_product(1, 100, "뷰티", availability="unknown"), _product(2, 100, "뷰티", availability="unknown"),
                _product(3, 100, "뷰티", availability="unavailable"), _product(4, 100, "뷰티", availability="available")]
    res = pipeline.build_pool(_rq(), products, 30, CV)
    assert res.product_ids == [1, 2, 4]                                        # unknown 유지 · unavailable만 제외


def test_pool_sorted_by_view_count_desc_then_product_id() -> None:
    products = [_product(1, 100, "뷰티", views=5), _product(2, 100, "뷰티", views=50), _product(3, 100, "뷰티", views=50),
                _product(4, 200, "주방", views=999), _product(5, 100, "뷰티", views=7, availability="unavailable")]
    res = pipeline.build_pool(_rq(disliked=[(200, "주방")]), products, 30, CV)
    assert res.product_ids == [2, 3, 1]                                        # 50, 50(동점 → id 순), 5 · 주방(999)은 제외 · 재고 없음 제외


def test_pool_matches_by_name_when_id_differs() -> None:
    rq = _rq(disliked=[(999, "완구")])                                         # ID 체계가 달라도 이름으로 걸러짐
    res = pipeline.build_pool(rq, PRODUCTS, 30, CV)
    assert all(p.categoryName != "완구" for p in PRODUCTS if p.productId in res.product_ids)


def test_pool_excludes_every_child_of_disliked_root_category() -> None:
    """비선호는 대분류로 온다(09-27 BE 결정) — 그 대분류 밑의 소분류 상품이 전부 빠지고 다른 대분류는 남는다."""
    products = [
        _product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=9),
        _product(2, 12, "메이크업", parent_id=1, parent_name="뷰티", views=8),
        _product(3, 21, "여성의류", parent_id=2, parent_name="패션", views=7),
        _product(4, 22, "가방·지갑", parent_id=2, parent_name="패션", views=6),
    ]
    picked = pipeline.build_pool(_rq(disliked=[(1, "뷰티")]), products, 30, CV).product_ids
    assert picked == [3, 4]


def test_pool_root_dislike_matches_by_parent_name_when_id_differs() -> None:
    """ID가 어긋나도 대분류 이름이 같으면 제외 — 소분류와 같은 보조 규칙."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티"), _product(2, 21, "여성의류", parent_id=2, parent_name="패션")]
    assert pipeline.build_pool(_rq(disliked=[(999, "뷰티")]), products, 30, CV).product_ids == [2]


def test_pool_without_parent_info_falls_back_to_leaf_only() -> None:
    """대분류 정보가 없는 상품(파일 카탈로그)은 소분류 판정만 받는다 — 대분류 비선호로는 빠지지 않는다(알려진 한계)."""
    products = [_product(1, 11, "스킨케어"), _product(2, 21, "여성의류")]
    assert pipeline.build_pool(_rq(disliked=[(1, "뷰티")]), products, 30, CV).product_ids == [1, 2]
    assert pipeline.build_pool(_rq(disliked=[(11, "스킨케어")]), products, 30, CV).product_ids == [2]


def test_pool_size_cap() -> None:
    assert len(pipeline.build_pool(_rq(), PRODUCTS, 5, CV).product_ids) == 5


@pytest.mark.parametrize("mode", list(pipeline.DislikeMode))
@pytest.mark.parametrize("pool_size", [2, 30])
@pytest.mark.parametrize("with_disliked_tag", [False, True])
def test_s0_disliked_tags_preserve_ranking_and_category_policy(mode, pool_size, with_disliked_tag) -> None:
    """태그만 하드 제외하고 기존 점수·카테고리 감점·리뷰 제외·개수 제한은 유지한다."""
    products = [
        _product(1, 10, "일반", views=100).model_copy(update={"brand": "소니"}),
        _product(2, 10, "일반", views=60), _product(3, 12, "선호", views=40),
        _product(4, 20, "비선호", views=20), _product(5, 10, "일반", views=0),
        _product(6, 10, "일반", views=1000, availability="unavailable"),
    ]
    rq = _rq(disliked=[(20, "비선호")], reviews=[Review(product_id=5, rating=3)])
    scores = [CategoryScore(category_id=12, n=3, score_sum=3)]  # 편향 0.6: 3번(1.6)이 2번(1.5)보다 앞
    tags = [selection.Tag("브랜드", "소니")] if with_disliked_tag else []
    result, rule = pipeline.make_pool(rq, products, pool_size, CV, dislike_mode=mode, scores=scores,
                                      pool_strategy=pipeline.PoolStrategy.S0, disliked_tags=tags)
    expected = [3, 2] if with_disliked_tag else [1, 3, 2]
    if mode is pipeline.DislikeMode.PENALTY:
        expected.append(4)
    assert result.product_ids == expected[:pool_size]
    assert result.catalog_version_id == CV and result.query_text == ""
    assert rule == (pipeline.POOL_RULE_V2 if mode is pipeline.DislikeMode.EXCLUDE else pipeline.POOL_RULE_V2_PENALTY)


# ---------------------------------------------------------------- build_pool — 비선호 감점(PENALTY, v2) · 10-11 결정

PENALTY = pipeline.DislikeMode.PENALTY


def test_pool_default_mode_is_exclude() -> None:
    """기본값은 v1 하드 규칙(제외) — 설정을 바꾸기 전까지 배포 동작이 그대로다."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=9), _product(2, 21, "여성의류", parent_id=2, parent_name="패션")]
    rq = _rq(disliked=[(1, "뷰티")])
    assert pipeline.build_pool(rq, products, 30, CV).product_ids == [2]
    assert pipeline.build_pool(rq, products, 30, CV, dislike_mode=pipeline.DislikeMode.EXCLUDE).product_ids == [2]


def test_pool_penalty_keeps_disliked_but_ranks_lower() -> None:
    """감점은 빼지 않고 뒤로 민다. 점수 = 인기(조회수 순위 백분위 × 2) + (비선호면 −2.0, DISLIKED_CATEGORY_SCORE).
    조회수 순위 100 > 60 > 30 > 10 → 인기 2.0 · 1.33 · 0.67 · 0. 뷰티 100회(비선호) → 2.0 − 2.0 = 0.0 = 꼴찌(3번, 0.0)와 동점 → 번호 순
    ⇒ [2, 4, 1, 3] — 조회수 1위여도 비선호는 맨 뒤로"""
    assert pipeline.DISLIKED_CATEGORY_SCORE == -2.0                              # 아래 수치는 이 값 기준
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=100),
                _product(2, 21, "여성의류", parent_id=2, parent_name="패션", views=60),
                _product(3, 22, "가방·지갑", parent_id=2, parent_name="패션", views=10),
                _product(4, 22, "가방·지갑", parent_id=2, parent_name="패션", views=30)]
    rq = _rq(disliked=[(1, "뷰티")])
    assert pipeline.build_pool(rq, products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 4, 1, 3]
    assert pipeline.build_pool(rq, products, 30, CV).product_ids == [2, 4, 3]   # 같은 입력, 제외 모드


def test_pool_penalty_without_dislikes_equals_exclude() -> None:
    """비선호가 없으면 두 모드의 결과가 같다 — 인기 점수가 조회수 순서를 그대로 따른다(회귀 안전)."""
    products = [_product(i, 100 + i % 3, f"c{i % 3}", views=(i * 37) % 11) for i in range(1, 41)]
    assert pipeline.build_pool(_rq(), products, 30, CV, dislike_mode=PENALTY).product_ids == pipeline.build_pool(_rq(), products, 30, CV).product_ids


def test_pool_penalty_never_empties_on_dislikes() -> None:
    """모든 대분류를 비선호로 넣어도(악성·장난 입력) 풀이 비지 않는다 — 감점끼리는 조회수 순서. 제외 모드는 0개."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=5),
                _product(2, 21, "여성의류", parent_id=2, parent_name="패션", views=9),
                _product(3, 22, "가방·지갑", parent_id=2, parent_name="패션", views=1, availability="unavailable")]
    rq = _rq(disliked=[(1, "뷰티"), (2, "패션")])
    assert pipeline.build_pool(rq, products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 1]   # 재고 없음은 여전히 제외
    assert pipeline.build_pool(rq, products, 30, CV).product_ids == []


def test_pool_penalty_all_zero_views_puts_disliked_last() -> None:
    """조회수가 전부 0이면(패키지 카탈로그) 인기 점수가 모두 0 — 비선호만 −2.0 이라 맨 뒤로, 나머지는 productId 순."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티"), _product(2, 21, "여성의류", parent_id=2, parent_name="패션"),
                _product(3, 12, "메이크업", parent_id=1, parent_name="뷰티"), _product(4, 22, "가방·지갑", parent_id=2, parent_name="패션")]
    assert pipeline.build_pool(_rq(disliked=[(1, "뷰티")]), products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 4, 1, 3]


def test_pool_penalty_matches_leaf_and_name_like_exclude() -> None:
    """비선호 판정 규칙(소분류 id · 이름 보조)은 두 모드가 같다 — 처리만 다르다."""
    products = [_product(1, 11, "스킨케어", views=9), _product(2, 21, "여성의류", views=6), _product(3, 21, "여성의류", views=3)]
    # 인기 2.0 · 1.0 · 0 → 1(이름으로 비선호): 2.0 − 2.0 = 0.0 = 3번과 동점 → 번호 순
    assert pipeline.build_pool(_rq(disliked=[(999, "스킨케어")]), products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 1, 3]
    assert pipeline.build_pool(_rq(), products, 30, CV, dislike_mode=PENALTY).product_ids == [1, 2, 3]   # 비선호가 없으면 원래 순서


# ---------------------------------------------------------------- build_pool — 리뷰 별점 편향 (v2 2단계 · 가이드 10-11 보강)
# 점수 = 조회수 ÷ 최대 × 2 + BIAS_WEIGHT(1.0) × 편향 (+ PENALTY 비선호면 −2.0). 편향 = score_sum ÷ (n + 2)

FOUR = [_product(1, 11, "스킨케어", views=100), _product(2, 11, "스킨케어", views=10),
        _product(3, 12, "메이크업", views=80), _product(4, 12, "메이크업", views=60)]


def test_pool_bias_moves_reviewed_category_forward_but_mixes() -> None:
    """메이크업(12) 5점·4점 → 편향 3 ÷ 4 = +0.75. 조회수 순위 100 > 80 > 60 > 10 → 인기 2.0 · 1.33 · 0.67 · 0.
    1: 2.0 · 2: 0 · 3: 1.33 + 0.75 = 2.08 · 4: 0.67 + 0.75 = 1.42 ⇒ [3, 1, 4, 2].
    조회수 1위인 스킨케어(1)가 여전히 2위 — 편향이 순서를 '섞을' 뿐 소분류가 풀을 독차지하지 않는다. EXCLUDE(기본)도 편향을 쓴다."""
    assert pipeline.BIAS_WEIGHT == 1.0                                          # 아래 수치는 이 값 기준
    scores = [CategoryScore(category_id=12, n=2, score_sum=3)]
    assert pipeline.build_pool(_rq(), FOUR, 30, CV).product_ids == [1, 3, 4, 2]                    # 리뷰 없음 = 조회수순
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores).product_ids == [3, 1, 4, 2]     # EXCLUDE + 편향
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores, dislike_mode=PENALTY).product_ids == [3, 1, 4, 2]   # 비선호 없으면 두 모드 같음


def test_rank_products_breakdown_sums_and_matches_build_pool() -> None:
    """rank_products — 점수 내역(인기 · 편향 · 감점)의 합이 총점이고, build_pool 의 30개는 그 앞부분이다(시나리오 검증 도구가 이 내역을 보여 준다)."""
    rq, scores = _rq(disliked=[(12, "메이크업")]), [CategoryScore(category_id=11, n=1, score_sum=2)]
    ranked = pipeline.rank_products(rq, FOUR, dislike_mode=PENALTY, scores=scores)
    assert all(abs(s.total - (s.popularity + s.bias + s.penalty)) < 1e-12 for s in ranked)
    assert [s.disliked for s in ranked if s.product.categoryId == 12] == [True, True]
    assert {s.penalty for s in ranked if s.disliked} == {pipeline.DISLIKED_CATEGORY_SCORE}
    assert [s.product.productId for s in ranked] == pipeline.build_pool(rq, FOUR, 30, CV, dislike_mode=PENALTY, scores=scores).product_ids
    assert [s.product.productId for s in pipeline.rank_products(rq, FOUR, scores=scores)] == [1, 2]   # 제외 모드는 비선호가 목록에 없음


def test_unknown_popularity_is_excluded_from_percentiles_and_preserved() -> None:
    known = [_product(1,11,"컵",views=0), _product(2,11,"컵",views=100), _product(3,11,"컵",views=200)]
    unknown = [ProductRecord.model_validate(_product(i,11,"컵").model_dump() |
                {"updatedAt":None,"viewCount":None,"availability":"unknown"},
                context={"catalog_source":"catalog-snapshot"}) for i in range(4,14)]
    ranked = pipeline.rank_products(_rq(), known + unknown)
    assert {s.product.productId:s.popularity for s in ranked if s.product.productId <= 3} == {1:0.,2:1.,3:2.}
    assert all(s.popularity == 0. for s in ranked if s.product.productId > 3)
    assert all(p.updatedAt is None and p.viewCount is None for p in unknown)
    assert all(s.popularity == 0. for s in pipeline.rank_products(_rq(), unknown))
    assert pipeline.build_pool(_rq(), list(reversed(unknown)), 30, CV).product_ids == list(range(4,14))


def test_pool_low_rating_pushes_back_not_excluded() -> None:
    """스킨케어(11) 1점 2건 → 편향 −4 ÷ 4 = −1.0. 1: 2.0 − 1 = 1.0 · 2: 0 − 1 = −1.0 · 3: 1.33 · 4: 0.67 ⇒ [3, 1, 4, 2]
    — 조회수 1위(1)가 2위로 밀리고 스킨케어 꼴찌(2)는 맨 뒤. 뒤로 갈 뿐 빠지지 않는다(규칙표 §5)."""
    scores = [CategoryScore(category_id=11, n=2, score_sum=-4)]
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores).product_ids == [3, 1, 4, 2]


def test_pool_penalty_disliked_category_adds_review_bias() -> None:
    """직접 고른 비선호는 −2.0 에서 출발하고 리뷰 편향을 그 위에 더한다(결정 D, 10-11).
    뷰티(1) 비선호 · 스킨케어(11) 5점 1건(+0.67). 조회수 50 · 50 · 10 → 인기 1.0 · 1.0 · 0.
    1(스킨케어): 1.0 − 2.0 + 0.67 = −0.33 · 2(메이크업, 리뷰 없음): 1.0 − 2.0 = −1.0 · 3(패션): 0 ⇒ [3, 1, 2].
    가산 덕에 스킨케어가 같은 비선호 대분류의 메이크업보다 앞 — 리뷰 없는 소분류엔 편향이 번지지 않는다. EXCLUDE 에선 뷰티 전체가 빠진다."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=50),
                _product(2, 12, "메이크업", parent_id=1, parent_name="뷰티", views=50),
                _product(3, 21, "여성의류", parent_id=2, parent_name="패션", views=10)]
    rq, scores = _rq(disliked=[(1, "뷰티")]), [CategoryScore(category_id=11, n=1, score_sum=2)]
    assert pipeline.build_pool(rq, products, 30, CV, scores=scores, dislike_mode=PENALTY).product_ids == [3, 1, 2]
    assert pipeline.build_pool(rq, products, 30, CV, scores=scores).product_ids == [3]


# ---------------------------------------------------------------- profile()


def test_profile_v1_happy_path() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=FakeRecipientStore(), pool_size=30)
    assert out.status is RunStatus.RESULT_READY
    assert out.validation.preferred_tags == [] and out.validation.disliked_category_names == ["뷰티"]
    assert out.search.catalog_version_id == CV and 0 < len(out.search.product_ids) <= 30
    assert out.validator_version == "v1-skip"
    assert store.get(9073) is out                                              # 저장됨


def test_profile_empty_pool_is_result_ready_with_empty_list(caplog) -> None:
    """비선호로 전부 걸러져 0개여도 실패가 아니다 — 빈 배열을 7.7로 보낸다(Backend 인기순 대체). 경고 로그 한 줄."""
    from profiling.backend import callback_body
    store = FakeStore()
    only_beauty = [_product(1, 100, "뷰티", views=5), _product(2, 100, "뷰티", views=3)]
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(only_beauty), store=store,
                               recipient_store=FakeRecipientStore(), pool_size=30)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == []
    assert callback_body(out).recommendedProductIds == []                       # 본문도 빈 배열 (스키마가 허용)
    assert any("풀 0개" in rec.message for rec in caplog.records)


def test_profile_penalty_mode_keeps_disliked_and_logs_rule(caplog) -> None:
    """profile() 이 dislike_mode 를 고르기(D)에 넘긴다 — 감점 모드에선 비선호 상품만 있어도 채움용으로 풀이 찬다. 규칙 라벨이 로그에 남는다."""
    only_beauty = [_product(1, 100, "뷰티", views=5), _product(2, 100, "뷰티", views=3)]
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(only_beauty), store=FakeStore(),
                               recipient_store=FakeRecipientStore(), pool_size=30, dislike_mode=PENALTY)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == [1, 2]
    assert out.validation.disliked_category_names == ["뷰티"]                               # 태그는 모드와 무관
    assert any(selection.POOL_RULE_D + "+penalty-fill" in rec.message for rec in caplog.records)
    assert any("비선호 2개를 감점 순서로 채움" in rec.message for rec in caplog.records)


def test_profile_overwrites_same_recipient() -> None:
    store = FakeStore(); cat = FakeCatalog(PRODUCTS)
    pipeline.profile(_rq(), catalog=cat, store=store, recipient_store=FakeRecipientStore())
    second = pipeline.profile(_rq(disliked=[(300, "완구")]), catalog=cat, store=store, recipient_store=FakeRecipientStore())
    assert store.get(9073) is second


def test_profile_no_catalog_is_failed_not_exception() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(), catalog=FakeCatalog([], fail=True), store=store, recipient_store=FakeRecipientStore())
    assert out.status is RunStatus.FAILED and "카탈로그" in out.failure_reason
    assert store.get(9073).status is RunStatus.FAILED                          # FAILED도 기록


BIAS_PRODUCTS = [_product(1, 11, "스킨케어", views=10), _product(2, 11, "스킨케어", views=0),
                 _product(3, 21, "여성의류", views=2), _product(4, 21, "여성의류", views=0)]


def test_profile_with_reviews_applies_bias_and_stores_scores(caplog) -> None:
    """리뷰 별점이 풀 순서에 들어가고(2단계) 점수·취향·리뷰가 프로필 행에 실린다. 리뷰한 상품 자체는 추천하지 않는다(10-12).
    조회수 10 · 0 · 2 · 0 · 1 → 인기 2.0(1) · 0(2) · 1.5(3) · 0(4) · 1.0(5). 리뷰 없으면 [1, 3, 5, 2, 4].
    여성의류(21) 상품 3·4에 5점 → 편향 +1.0: 3 = 2.5 · 1 = 2.0 · 5 = 2.0 · 4 = 1.0 · 2 = 0. 리뷰한 3·4는 빠지고 [1, 5, 2] —
    리뷰 안 한 같은 소분류 상품 5가 조회수 0인 2를 앞지른다(동점 1·5 는 상품 번호 순)."""
    products = BIAS_PRODUCTS + [_product(5, 21, "여성의류", views=1)]
    rstore = FakeRecipientStore()
    rq = _rq(pref="휴대용", reviews=[Review(product_id=3, rating=5, review_text="좋아요"), Review(product_id=4, rating=5)])
    no_reviews = _rq(disliked=[(99, "관계없는 대분류")])                            # 입력이 전부 비면 0개라(결정 C) 무관한 비선호 하나
    assert pipeline.profile(no_reviews, catalog=FakeCatalog(products), store=FakeStore(),
                            recipient_store=FakeRecipientStore()).search.product_ids == [1, 3, 5, 2, 4]
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out = pipeline.profile(rq, catalog=FakeCatalog(products), store=FakeStore(), recipient_store=rstore)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == [1, 5, 2]
    assert out.validation.preferred_tags == []                                  # 글·취향 → 태그는 하네스 뒤
    row = rstore.get(9073)
    assert row.category_scores == [CategoryScore(category_id=21, n=2, score_sum=4)]
    assert row.gift_preference_hash == preference_hash("휴대용") and [r.product_id for r in row.reviews] == [3, 4]
    assert [r.text_clean for r in row.reviews] == ["좋아요", None] and row.tag_status == "off"   # 정리본만 저장(DR-039) · 추출기 꺼짐
    assert any("scores=1" in r.message and selection.POOL_RULE_D in r.message for r in caplog.records)
    assert not any("좋아요" in r.message for r in caplog.records)               # 리뷰 원문은 로그에 남지 않는다


def test_profile_reviews_not_in_catalog_warn(caplog) -> None:
    """리뷰 상품이 카탈로그에 없으면 편향 0 — 결과는 리뷰 없을 때와 같고 경고에 개수가 찍힌다(보강 C)."""
    rq = _rq(reviews=[Review(product_id=999, rating=5)])
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out = pipeline.profile(rq, catalog=FakeCatalog(BIAS_PRODUCTS), store=FakeStore(), recipient_store=FakeRecipientStore())
    assert out.search.product_ids == [1, 3, 2, 4]
    assert any("0/1" in r.message for r in caplog.records)


def test_profile_finds_category_from_active_snapshot_not_by_id() -> None:
    """소분류는 active() 로 받은 products 에서 찾는다 — by_id 는 요청 도중 다른 카탈로그 버전을 줄 수 있다(보강 B)."""
    class NoByIdCatalog(FakeCatalog):
        def by_id(self, product_id):
            raise AssertionError("by_id 를 쓰면 안 된다")

    rstore = FakeRecipientStore()
    out = pipeline.profile(_rq(reviews=[Review(product_id=2, rating=5)]), catalog=NoByIdCatalog(BIAS_PRODUCTS), store=FakeStore(),
                           recipient_store=rstore)
    assert out.status is RunStatus.RESULT_READY and rstore.get(9073).category_scores[0].category_id == 11


def test_profile_same_input_same_pool_with_bias() -> None:
    """편향이 소수여도 같은 입력이면 같은 결과(결정 a — 임의성 없음)."""
    products = [_product(i, 100 + i % 4, f"c{i % 4}", views=(i * 13) % 17) for i in range(1, 61)]
    rq = _rq(reviews=[Review(product_id=1, rating=5), Review(product_id=2, rating=2), Review(product_id=7, rating=4)])
    run = lambda: pipeline.profile(rq, catalog=FakeCatalog(products), store=FakeStore(), recipient_store=FakeRecipientStore()).search.product_ids
    assert run() == run()


def test_profile_store_failure_is_failed() -> None:
    class BrokenStore(FakeStore):
        def save(self, outcome):
            raise RuntimeError("db down")
    out = pipeline.profile(_rq(), catalog=FakeCatalog(PRODUCTS), store=BrokenStore(), recipient_store=FakeRecipientStore())
    assert out.status is RunStatus.FAILED and "저장 실패" in out.failure_reason


def test_profile_records_running_then_result_with_same_hash() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=FakeRecipientStore())
    assert store.history == [RunStatus.RUNNING, RunStatus.RESULT_READY]          # 접수 기록이 결과보다 먼저
    assert out.input_hash == pipeline.input_hash(_rq(disliked=[(100, "뷰티")])) and len(out.input_hash) == 64


def test_input_hash_ignores_disliked_order_but_not_content() -> None:
    a = pipeline.input_hash(_rq(disliked=[(100, "뷰티"), (200, "주방")]))
    assert a == pipeline.input_hash(_rq(disliked=[(200, "주방"), (100, "뷰티")]))   # 순서만 다름 → 같은 입력
    assert a != pipeline.input_hash(_rq(disliked=[(100, "뷰티")]))                  # 내용 다름 → 다른 입력


def test_profile_upserts_recipient_profile() -> None:
    store, rstore = FakeStore(), FakeRecipientStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=rstore)
    row = rstore.get(9073)
    assert out.status is RunStatus.RESULT_READY and row is not None
    assert row.source_version == 3 and [c.category_name for c in row.disliked_categories] == ["뷰티"]
    assert row.preferred_tags == [] and row.recommended_product_ids == out.search.product_ids


def test_profile_recipient_store_failure_is_failed_no_callback() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=FakeRecipientStore(fail=True))
    assert out.status is RunStatus.FAILED and "프로필 저장 실패" in out.failure_reason
    assert store.history[-1] is RunStatus.FAILED                                  # 실행 기록도 FAILED로 되돌림


@pytest.mark.parametrize("stage,code,history", [
    ("running", ErrorCode.STORE_FAILED, []),
    ("result", ErrorCode.STORE_FAILED, [RunStatus.RUNNING]),
    ("recipient", ErrorCode.STORE_FAILED, [RunStatus.RUNNING, RunStatus.RESULT_READY, RunStatus.FAILED]),
    ("pipeline", ErrorCode.PIPELINE_ERROR, [RunStatus.RUNNING, RunStatus.FAILED]),
    ("failed_save", ErrorCode.PIPELINE_ERROR, [RunStatus.RUNNING]),
    ("catalog", ErrorCode.NO_ACTIVE_CATALOG, [RunStatus.RUNNING, RunStatus.FAILED]),
])
def test_failure_reason_and_debug_logs_omit_exception_text(stage, code, history, monkeypatch, caplog) -> None:
    """예외 첫 줄과 DEBUG traceback에도 입력이 들어갈 수 있다. 상태 전이는 그대로 둔다."""
    marker = "SYNTHETIC_PRIVATE_PAYLOAD_97531"
    catalog, store, recipients = FakeCatalog(PRODUCTS), FakeStore(), FakeRecipientStore()

    def fail(*args, **kwargs):
        error = NoActiveCatalog if stage == "catalog" else RuntimeError
        raise error(marker + "\nDETAIL: " + marker)

    save = store.save

    def save_or_fail(outcome):
        failed_status = {"running": RunStatus.RUNNING, "result": RunStatus.RESULT_READY, "failed_save": RunStatus.FAILED}.get(stage)
        if outcome.status is failed_status:
            fail()
        save(outcome)

    monkeypatch.setattr(store, "save", save_or_fail)
    if stage == "recipient":
        monkeypatch.setattr(recipients, "upsert", fail)
    elif stage in ("pipeline", "failed_save"):
        monkeypatch.setattr(pipeline, "make_pool", fail)
    elif stage == "catalog":
        monkeypatch.setattr(catalog, "active", fail)
    with caplog.at_level("DEBUG", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=catalog, store=store, recipient_store=recipients)
    assert out.status is RunStatus.FAILED and out.failure_code is code and out.search is None
    assert store.history == history and not recipients.rows
    assert marker not in out.failure_reason and marker not in caplog.text
    assert all(marker not in row.model_dump_json() for row in store.saved.values())
    records = [record for record in caplog.records if record.name == "profiling.pipeline"]
    assert records and all(record.exc_info is None for record in records)
    assert ("NoActiveCatalog" if stage == "catalog" else "RuntimeError") in out.failure_reason


@pytest.mark.parametrize("classified", [False, True])
def test_tagger_errors_keep_fallback_without_logging_exception_text(classified, caplog) -> None:
    marker = "SYNTHETIC_PRIVATE_TAGGER_97531"

    class BrokenTagger:
        model = "synthetic"

        def extract(self, system, prompt, schema):
            if classified:
                raise TagExtractError("timeout", marker)
            raise RuntimeError(marker)

    store, recipients = FakeStore(), FakeRecipientStore()
    with caplog.at_level("DEBUG", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(pref="캠핑 선물을 좋아해요"), catalog=FakeCatalog(PRODUCTS), store=store,
                               recipient_store=recipients, tagger=BrokenTagger())
    assert out.status is RunStatus.RESULT_READY and out.failure_code is None
    assert out.tag_status == ("fail:timeout" if classified else "fail:unknown")
    assert store.history == [RunStatus.RUNNING, RunStatus.RESULT_READY] and recipients.get(9073) is not None
    assert marker not in caplog.text and marker not in out.model_dump_json()
    assert all(record.exc_info is None for record in caplog.records if record.name == "profiling.pipeline")


# ---------------------------------------------------------------- 바뀐 내용 로그(profile_change, 10-15 C안)


class _GetFails(FakeRecipientStore):
    def get(self, rid):
        raise RuntimeError("profiles down")


def test_profile_change_log_first_then_update_without_text(caplog) -> None:
    """같은 사람의 두 번째 분석은 이전 프로필과 비교한 한 줄을 남긴다 — 번호 · 개수만, 취향 · 리뷰 글은 없다."""
    products = BIAS_PRODUCTS + [_product(5, 21, "여성의류", views=1)]
    rstore = FakeRecipientStore()
    first = _rq(pref="휴대용 선물", reviews=[Review(product_id=3, rating=5, review_text="좋아요")])
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        pipeline.profile(first, catalog=FakeCatalog(products), store=FakeStore(), recipient_store=rstore)
    line = next(r.message for r in caplog.records if r.message.startswith("profile_change"))
    assert "source_version=3 first=yes" in line and "pref=new" in line and "reviews_added=3" in line
    caplog.clear()
    second = ProfileRequest(recipient_user_id=9073, source_version=4, gift_preference=None,
                            disliked_categories=[DislikedCategory(category_id=99, category_name="관계없는 대분류")],
                            reviews=[Review(product_id=3, rating=1, review_text="별로예요"), Review(product_id=4, rating=5)])
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out = pipeline.profile(second, catalog=FakeCatalog(products), store=FakeStore(), recipient_store=rstore)
    assert out.status is RunStatus.RESULT_READY
    line = next(r.message for r in caplog.records if r.message.startswith("profile_change"))
    assert "first=no prev_version=3" in line and "input_changed=yes" in line and "pref=removed" in line
    assert "reviews=+1/-0/~1" in line and "reviews_added=4" in line and "rating_changed=3" in line and "dislikes_added=99" in line
    assert not any(w in r.message for r in caplog.records for w in ("휴대용", "좋아요", "별로예요"))   # 글은 로그에 없다


def test_profile_change_log_get_failure_does_not_block_profile(caplog) -> None:
    """이전 프로필을 못 읽으면 비교만 건너뛰고 저장 · 결과는 그대로."""
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(pref="휴대용"), catalog=FakeCatalog(PRODUCTS), store=FakeStore(), recipient_store=_GetFails())
    assert out.status is RunStatus.RESULT_READY
    assert any("이전 프로필 조회 실패" in r.message for r in caplog.records)
