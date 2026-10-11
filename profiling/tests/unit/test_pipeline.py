"""pipeline — 가짜 adapter(ports 모양)로 v1 흐름. DB·HTTP 없음."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from profiling import pipeline
from profiling.ports import NoActiveCatalog
from profiling.schemas import ProductRecord, ProfileExtractRequest
from profiling.types import (
    CategoryScore,
    DislikedCategory,
    ProfileRequest,
    Review,
    RunStatus,
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


# ---------------------------------------------------------------- build_pool — 비선호 감점(PENALTY, v2) · 10-11 결정

PENALTY = pipeline.DislikeMode.PENALTY


def test_pool_default_mode_is_exclude() -> None:
    """기본값은 v1 하드 규칙(제외) — 설정을 바꾸기 전까지 배포 동작이 그대로다."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=9), _product(2, 21, "여성의류", parent_id=2, parent_name="패션")]
    rq = _rq(disliked=[(1, "뷰티")])
    assert pipeline.build_pool(rq, products, 30, CV).product_ids == [2]
    assert pipeline.build_pool(rq, products, 30, CV, dislike_mode=pipeline.DislikeMode.EXCLUDE).product_ids == [2]


def test_pool_penalty_keeps_disliked_but_ranks_lower() -> None:
    """감점은 빼지 않고 뒤로 민다. 점수 = 조회수 ÷ 최대 × 2 + (비선호면 −2.0, DISLIKED_CATEGORY_SCORE).
    뷰티 100회(비선호) → 2.0 − 2.0 = 0.0 · 패션 60회 → 1.2 · 패션 10회 → 0.2  ⇒ [2, 3, 1] — 조회수 최대여도 비선호는 맨 뒤"""
    assert pipeline.DISLIKED_CATEGORY_SCORE == -2.0                              # 아래 수치는 이 값 기준
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=100),
                _product(2, 21, "여성의류", parent_id=2, parent_name="패션", views=60),
                _product(3, 22, "가방·지갑", parent_id=2, parent_name="패션", views=10)]
    rq = _rq(disliked=[(1, "뷰티")])
    assert pipeline.build_pool(rq, products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 3, 1]
    assert pipeline.build_pool(rq, products, 30, CV).product_ids == [2, 3]      # 같은 입력, 제외 모드


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
    products = [_product(1, 11, "스킨케어", views=9), _product(2, 21, "여성의류", views=6)]   # 1: 2.0 − 2.0 = 0.0 · 2: 1.33
    assert pipeline.build_pool(_rq(disliked=[(999, "스킨케어")]), products, 30, CV, dislike_mode=PENALTY).product_ids == [2, 1]
    assert pipeline.build_pool(_rq(), products, 30, CV, dislike_mode=PENALTY).product_ids == [1, 2]   # 비선호가 없으면 원래 순서


# ---------------------------------------------------------------- build_pool — 리뷰 별점 편향 (v2 2단계 · 가이드 10-11 보강)
# 점수 = 조회수 ÷ 최대 × 2 + BIAS_WEIGHT(1.0) × 편향 (+ PENALTY 비선호면 −2.0). 편향 = score_sum ÷ (n + 2)

FOUR = [_product(1, 11, "스킨케어", views=100), _product(2, 11, "스킨케어", views=10),
        _product(3, 12, "메이크업", views=80), _product(4, 12, "메이크업", views=60)]


def test_pool_bias_moves_reviewed_category_forward_but_mixes() -> None:
    """메이크업(12) 5점 1건 → 편향 +0.67. 1: 2.0 · 2: 0.2 · 3: 1.6 + 0.67 = 2.27 · 4: 1.2 + 0.67 = 1.87 ⇒ [3, 1, 4, 2].
    조회수 최대인 스킨케어(1)가 여전히 2위 — 편향이 순서를 '섞을' 뿐 소분류가 풀을 독차지하지 않는다. EXCLUDE(기본)도 편향을 쓴다."""
    assert pipeline.BIAS_WEIGHT == 1.0                                          # 아래 수치는 이 값 기준
    scores = [CategoryScore(category_id=12, n=1, score_sum=2)]
    assert pipeline.build_pool(_rq(), FOUR, 30, CV).product_ids == [1, 3, 4, 2]                    # 리뷰 없음 = 조회수순
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores).product_ids == [3, 1, 4, 2]     # EXCLUDE + 편향
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores, dislike_mode=PENALTY).product_ids == [3, 1, 4, 2]   # 비선호 없으면 두 모드 같음


def test_pool_low_rating_pushes_back_not_excluded() -> None:
    """스킨케어(11) 1점 2건 → 편향 −4 ÷ 4 = −1.0. 1: 1.0 · 2: −0.8 · 3: 1.6 · 4: 1.2 ⇒ [3, 4, 1, 2] — 뒤로 갈 뿐 빠지지 않는다(규칙표 §5)."""
    scores = [CategoryScore(category_id=11, n=2, score_sum=-4)]
    assert pipeline.build_pool(_rq(), FOUR, 30, CV, scores=scores).product_ids == [3, 4, 1, 2]


def test_pool_penalty_disliked_category_adds_review_bias() -> None:
    """직접 고른 비선호는 −2.0 에서 출발하고 리뷰 편향을 그 위에 더한다(결정 D, 10-11).
    뷰티(1) 비선호 · 스킨케어(11) 5점 1건(+0.67). 1(스킨케어): 2.0 − 2.0 + 0.67 = 0.67 · 2(메이크업, 리뷰 없음): 2.0 − 2.0 = 0 · 3(패션): 0.4
    ⇒ [1, 3, 2]. 같은 비선호 대분류라도 리뷰 없는 소분류(메이크업)엔 편향이 번지지 않는다. EXCLUDE 에선 뷰티 전체가 빠진다."""
    products = [_product(1, 11, "스킨케어", parent_id=1, parent_name="뷰티", views=50),
                _product(2, 12, "메이크업", parent_id=1, parent_name="뷰티", views=50),
                _product(3, 21, "여성의류", parent_id=2, parent_name="패션", views=10)]
    rq, scores = _rq(disliked=[(1, "뷰티")]), [CategoryScore(category_id=11, n=1, score_sum=2)]
    assert pipeline.build_pool(rq, products, 30, CV, scores=scores, dislike_mode=PENALTY).product_ids == [1, 3, 2]
    assert pipeline.build_pool(rq, products, 30, CV, scores=scores).product_ids == [3]


# ---------------------------------------------------------------- profile()


def test_profile_v1_happy_path() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=FakeRecipientStore(), pool_size=30)
    assert out.status is RunStatus.RESULT_READY
    assert out.validation.preferred_tags == [] and out.validation.disliked_tags == ["뷰티"]
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
    """profile() 이 dislike_mode 를 build_pool 에 넘긴다 — 감점 모드에선 비선호 상품만 있어도 풀이 찬다. 규칙 라벨이 로그에 남는다."""
    only_beauty = [_product(1, 100, "뷰티", views=5), _product(2, 100, "뷰티", views=3)]
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(only_beauty), store=FakeStore(),
                               recipient_store=FakeRecipientStore(), pool_size=30, dislike_mode=PENALTY)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == [1, 2]
    assert out.validation.disliked_tags == ["뷰티"]                               # 태그는 모드와 무관
    assert any(pipeline.POOL_RULE_V2_PENALTY in rec.message for rec in caplog.records)


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
    """리뷰 별점이 풀 순서에 들어가고(2단계) 점수·취향·리뷰가 프로필 행에 실린다. 태그(모델)는 아직 없다.
    상품 2(스킨케어) 5점 → 스킨케어 편향 +0.67. 리뷰 없으면 [1, 3, 2, 4](2.0 · 0.4 · 0 · 0) → 있으면 2가 0.67로 3(0.4)을 앞지른다."""
    rstore = FakeRecipientStore()
    rq = _rq(pref="휴대용", reviews=[Review(product_id=2, rating=5, review_text="좋아요")])
    assert pipeline.profile(_rq(), catalog=FakeCatalog(BIAS_PRODUCTS), store=FakeStore(),
                            recipient_store=FakeRecipientStore()).search.product_ids == [1, 3, 2, 4]
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out = pipeline.profile(rq, catalog=FakeCatalog(BIAS_PRODUCTS), store=FakeStore(), recipient_store=rstore)
    assert out.status is RunStatus.RESULT_READY and out.search.product_ids == [1, 2, 3, 4]
    assert out.validation.preferred_tags == []                                  # 글·취향 → 태그는 하네스 뒤
    row = rstore.get(9073)
    assert row.category_scores == [CategoryScore(category_id=11, n=1, score_sum=2)]
    assert row.gift_preference == "휴대용" and [r.product_id for r in row.reviews] == [2]
    assert any("scores=1" in r.message and pipeline.POOL_RULE_V2 in r.message for r in caplog.records)
    assert not any("좋아요" in r.message for r in caplog.records)               # 리뷰 원문은 로그에 남지 않는다


def test_profile_reviews_not_in_catalog_warn_with_provisional_hint(caplog) -> None:
    """임시 번호 카탈로그(BE id-map 전)면 BE 번호인 리뷰가 하나도 안 맞는다 — 편향 0, 결과는 리뷰 없을 때와 같고 경고에 원인이 찍힌다(보강 C)."""
    class ProvisionalCatalog(FakeCatalog):
        provisional_ids = True

    rq = _rq(reviews=[Review(product_id=999, rating=5)])
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out = pipeline.profile(rq, catalog=ProvisionalCatalog(BIAS_PRODUCTS), store=FakeStore(), recipient_store=FakeRecipientStore())
    assert out.search.product_ids == [1, 3, 2, 4]
    assert any("0/1" in r.message and "임시 번호" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        pipeline.profile(rq, catalog=FakeCatalog(BIAS_PRODUCTS), store=FakeStore(), recipient_store=FakeRecipientStore())
    assert any("0/1" in r.message for r in caplog.records) and not any("임시 번호" in r.message for r in caplog.records)


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
    assert row.disliked_tags == ["뷰티"] and row.preferred_tags == [] and row.recommended_product_ids == out.search.product_ids


def test_profile_recipient_store_failure_is_failed_no_callback() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(), catalog=FakeCatalog(PRODUCTS), store=store, recipient_store=FakeRecipientStore(fail=True))
    assert out.status is RunStatus.FAILED and "프로필 저장 실패" in out.failure_reason
    assert store.history[-1] is RunStatus.FAILED                                  # 실행 기록도 FAILED로 되돌림
