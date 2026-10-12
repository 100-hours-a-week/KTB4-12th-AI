"""selection.select_pool — 풀 고르기 D(10-12 결정): 비선호 하드 제외 · 한 소분류 최대 10 · 같은 모델 하나 · 모자랄 때만 비선호 채움 · 임베딩 경로."""

from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from profiling import pipeline, selection
from profiling.schemas import ProductRecord
from profiling.types import DislikedCategory, ProfileRequest

CV = UUID("00000000-0000-0000-0000-000000000001")
PENALTY, EXCLUDE = pipeline.DislikeMode.PENALTY, pipeline.DislikeMode.EXCLUDE


def _p(pid: int, cat: int, views: int, *, parent: int = 1, name: str | None = None, brand: str = "b") -> ProductRecord:
    return ProductRecord(productId=pid, name=name or f"상품 {pid}", brand=brand, description=None, categoryId=cat, categoryName=f"c{cat}",
                         parentCategoryId=parent, parentCategoryName=f"대{parent}", price=1000, availability="available",
                         updatedAt=datetime(2026, 10, 1, tzinfo=UTC), viewCount=views)


def _rq(disliked=()) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=1, source_version=1, gift_preference=None,
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked], reviews=[])


def _pick(products, pool, mode=EXCLUDE, rq=None, **kw):
    ranked = pipeline.rank_products(rq or _rq(), products, dislike_mode=mode)
    return selection.select_pool(ranked, pool, allow_disliked=mode is PENALTY, **kw)


def test_caps_one_subcategory_at_ten() -> None:
    """조회수 상위 15개가 모두 소분류 10 이면 S0 는 15개를 다 넣지만 D 는 10개까지만, 나머지는 다음 소분류로."""
    products = [_p(i, 10, 1000 - i) for i in range(1, 16)] + [_p(100 + i, 20, 500 - i) for i in range(1, 11)]
    sel = _pick(products, 20)
    cats = [p.categoryId for p in products for pid in sel.product_ids if p.productId == pid]
    assert len(sel.product_ids) == 20 and cats.count(10) == selection.CAT_CAP and cats.count(20) == 10
    s0 = pipeline.build_pool(_rq(), products, 20, CV).product_ids
    assert sum(pid < 100 for pid in s0) == 15


def test_cap_is_relaxed_only_when_pool_would_be_short() -> None:
    products = [_p(i, 10, 100 - i) for i in range(1, 13)]                 # 소분류 하나뿐
    assert _pick(products, 12).product_ids == list(range(1, 13))


def test_same_brand_same_model_code_kept_once() -> None:
    names = ["SONY WH-1000XM6 헤드폰 올리브", "SONY WH-1000XM6 헤드폰 샌드", "SONY WF-1000XM6 이어폰", "케이크 세트"]
    products = [_p(i + 1, 10 + i, 100 - i, name=n, brand="소니" if i < 3 else "빵집") for i, n in enumerate(names)]
    sel = _pick(products, 4)
    assert sel.product_ids == [1, 3, 4] and sel.dups_skipped >= 1          # 색상만 다른 2번은 빠지고, 다른 모델 3번은 남는다
    assert selection.dup_key("케이크 세트", "빵집") is None


def test_disliked_excluded_while_enough_and_filled_last_in_penalty_mode() -> None:
    """비선호(대분류 9)가 조회수 1~3위라도 비선호 아닌 상품으로 채울 수 있으면 0개. 모자라면 감점 순서로 뒤에 채운다(PENALTY)."""
    liked = [_p(10 + i, 10 + i, 50 - i) for i in range(3)]
    disliked = [_p(i, 90, 100 - i, parent=9) for i in range(1, 4)]
    rq = _rq(disliked=[(9, "대9")])
    assert _pick(liked + disliked, 3, PENALTY, rq).product_ids == [10, 11, 12]
    sel = _pick(liked + disliked, 5, PENALTY, rq)
    assert sel.product_ids == [10, 11, 12, 1, 2] and sel.filled_disliked == 2
    assert _pick(liked + disliked, 5, EXCLUDE, rq).product_ids == [10, 11, 12]   # 제외 모드는 채우지 않는다


def test_rank_order_kept_without_tags() -> None:
    """태그 · 임베딩이 없으면 상한 · 중복에 걸리지 않는 한 순위 그대로 — 그래서 리뷰 없는 사람은 v1 과 같다."""
    products = [_p(i, i, 100 - i) for i in range(1, 8)]
    assert _pick(products, 5).product_ids == pipeline.build_pool(_rq(), products, 5, CV).product_ids
    assert _pick(products, 5).rule == selection.POOL_RULE_D and _pick(products, 5).query_text == ""


class FakeEmbedding:
    """소분류 c 의 상품은 축 c 방향, 질의 '소분류 c' 도 축 c 방향."""

    version = "fake"

    def __init__(self, cat_of: dict[int, int], dim: int = 8) -> None:
        self.cat_of, self.dim = cat_of, dim

    def product_vectors(self, ids):
        return np.stack([np.eye(self.dim)[self.cat_of[i] % self.dim] for i in ids])

    def encode(self, texts):
        return np.stack([np.eye(self.dim)[int(t.split()[-1]) % self.dim] for t in texts])


def test_embedding_path_follows_tags_with_cap_and_is_deterministic() -> None:
    """태그가 소분류 3 을 가리키면 조회수가 낮아도 소분류 3 상품이 들어온다 — 단 최대 10개. 같은 입력이면 같은 결과.
    (가짜 벡터는 같은 소분류끼리 코사인 1 이라 MMR 이 다른 소분류와 번갈아 고른다 — 실제 벡터에선 덜하다)"""
    products = [_p(i, 1, 1000 - i) for i in range(1, 21)] + [_p(100 + i, 3, 10 + i) for i in range(1, 16)]
    emb = FakeEmbedding({p.productId: p.categoryId for p in products})
    tags = [selection.Tag("카테고리", "소분류 3")]
    a, b = _pick(products, 20, tags=tags, embedding=emb), _pick(products, 20, tags=tags, embedding=emb)
    assert a == b and a.rule == selection.POOL_RULE_D_EMBED and a.query_text == "소분류 3" and a.candidates > 0
    assert sum(pid > 100 for pid in a.product_ids) == selection.CAT_CAP                              # 태그 소분류가 상한까지 들어옴
    assert not any(pid > 100 for pid in pipeline.build_pool(_rq(), products, 20, CV).product_ids)      # S0 는 조회수 낮아 하나도 없음


def test_embedding_never_brings_back_disliked() -> None:
    """비선호 카테고리 상품과 태그가 가장 가까워도(U09 꼴) 검색은 비선호 밖에서만 한다."""
    products = [_p(i, 5, 100 - i, parent=9) for i in range(1, 6)] + [_p(10 + i, 6, 50 - i) for i in range(5)]
    emb = FakeEmbedding({p.productId: p.categoryId for p in products})
    sel = _pick(products, 5, PENALTY, _rq(disliked=[(9, "대9")]), tags=[selection.Tag("카테고리", "소분류 5")], embedding=emb)
    assert sel.product_ids == [10, 11, 12, 13, 14] and sel.filled_disliked == 0


def test_profile_uses_d_by_default_and_s0_on_switch() -> None:
    from tests.unit.test_pipeline import FakeCatalog, FakeRecipientStore, FakeStore

    products = [_p(i, 10, 1000 - i) for i in range(1, 16)] + [_p(100 + i, 20, 500 - i) for i in range(1, 11)]
    run = lambda **kw: pipeline.profile(_rq(), catalog=FakeCatalog(products), store=FakeStore(), recipient_store=FakeRecipientStore(),
                                        pool_size=20, **kw).search.product_ids
    assert sum(pid < 100 for pid in run()) == selection.CAT_CAP
    assert run(pool_strategy=pipeline.PoolStrategy.S0) == pipeline.build_pool(_rq(), products, 20, CV).product_ids


def test_reviewed_products_are_never_recommended() -> None:
    """리뷰한 상품(이미 받은 선물)은 D · S0 어느 쪽에서도 고르지 않는다 — 비선호 채움에서도(10-12)."""
    from profiling.types import Review

    products = [_p(i, 10 + i % 3, 100 - i) for i in range(1, 8)] + [_p(50, 90, 200, parent=9)]
    rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference=None,
                        disliked_categories=[DislikedCategory(category_id=9, category_name="대9")],
                        reviews=[Review(product_id=1, rating=5), Review(product_id=50, rating=5)])
    for strategy in (pipeline.PoolStrategy.D, pipeline.PoolStrategy.S0):
        ids = pipeline.make_pool(rq, products, 30, CV, dislike_mode=PENALTY, pool_strategy=strategy)[0].product_ids
        assert 1 not in ids and 50 not in ids and len(ids) == 6
