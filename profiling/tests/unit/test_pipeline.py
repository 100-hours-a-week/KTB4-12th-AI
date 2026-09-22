"""pipeline — 가짜 adapter(ports 모양)로 v1 흐름. DB·HTTP 없음."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from profiling.profile import pipeline
from profiling.profile.ports import NoActiveCatalog
from profiling.profile.types import DislikedCategory, ProfileRequest, Review, RunStatus
from profiling.transport.schemas import ProductRecord, ProfileExtractRequest

CV = UUID(int=7)   # 시험용 카탈로그 버전 ID

# ---------------------------------------------------------------- 가짜


def _product(pid: int, cat_id: int, cat_name: str, available: bool = True, views: int = 0) -> ProductRecord:
    return ProductRecord(productId=pid, name=f"p{pid}", brand="b", description=None, categoryId=cat_id, categoryName=cat_name,
                         price=1000, available=available, updatedAt=datetime(2026, 9, 21, tzinfo=UTC), viewCount=views)


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


# 카테고리 3종 × 판매중/불가 섞어 40개: 100번대=뷰티, 200번대=주방, 300번대=완구
PRODUCTS = [_product(pid, cat, name, available=(pid % 10 != 0))
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
    assert all(by_id[i].categoryId != 200 and by_id[i].available for i in res.product_ids)
    assert res.product_ids == sorted(res.product_ids)                          # 조회수가 전부 0이면 카탈로그 순서 (동점 규칙)
    assert len(res.product_ids) == min(30, sum(1 for p in PRODUCTS if p.available and p.categoryId != 200))


def test_pool_sorted_by_view_count_desc_then_product_id() -> None:
    products = [_product(1, 100, "뷰티", views=5), _product(2, 100, "뷰티", views=50), _product(3, 100, "뷰티", views=50),
                _product(4, 200, "주방", views=999), _product(5, 100, "뷰티", views=7, available=False)]
    res = pipeline.build_pool(_rq(disliked=[(200, "주방")]), products, 30, CV)
    assert res.product_ids == [2, 3, 1]                                        # 50, 50(동점 → id 순), 5 · 주방(999)은 제외 · 판매불가 제외


def test_pool_matches_by_name_when_id_differs() -> None:
    rq = _rq(disliked=[(999, "완구")])                                         # ID 체계가 달라도 이름으로 걸러짐
    res = pipeline.build_pool(rq, PRODUCTS, 30, CV)
    assert all(p.categoryName != "완구" for p in PRODUCTS if p.productId in res.product_ids)


def test_pool_size_cap() -> None:
    assert len(pipeline.build_pool(_rq(), PRODUCTS, 5, CV).product_ids) == 5


# ---------------------------------------------------------------- profile()


def test_profile_v1_happy_path() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store, pool_size=30)
    assert out.status is RunStatus.RESULT_READY
    assert out.validation.preferred_tags == [] and out.validation.disliked_tags == ["뷰티"]
    assert out.search.catalog_version_id == CV and 0 < len(out.search.product_ids) <= 30
    assert out.validator_version == "v1-skip"
    assert store.get(9073) is out                                              # 저장됨


def test_profile_overwrites_same_recipient() -> None:
    store = FakeStore(); cat = FakeCatalog(PRODUCTS)
    pipeline.profile(_rq(), catalog=cat, store=store)
    second = pipeline.profile(_rq(disliked=[(300, "완구")]), catalog=cat, store=store)
    assert store.get(9073) is second


def test_profile_no_catalog_is_failed_not_exception() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(), catalog=FakeCatalog([], fail=True), store=store)
    assert out.status is RunStatus.FAILED and "카탈로그" in out.failure_reason
    assert store.get(9073).status is RunStatus.FAILED                          # FAILED도 기록


def test_profile_with_reviews_still_v1_path() -> None:
    out = pipeline.profile(_rq(pref="휴대용", reviews=[Review(product_id=1, rating=5)]), catalog=FakeCatalog(PRODUCTS), store=FakeStore())
    assert out.status is RunStatus.RESULT_READY and out.validation.preferred_tags == []   # 모델 단계 미구현 → v1 경로


def test_profile_store_failure_is_failed() -> None:
    class BrokenStore(FakeStore):
        def save(self, outcome):
            raise RuntimeError("db down")
    out = pipeline.profile(_rq(), catalog=FakeCatalog(PRODUCTS), store=BrokenStore())
    assert out.status is RunStatus.FAILED and "저장 실패" in out.failure_reason


def test_profile_records_running_then_result_with_same_hash() -> None:
    store = FakeStore()
    out = pipeline.profile(_rq(disliked=[(100, "뷰티")]), catalog=FakeCatalog(PRODUCTS), store=store)
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
