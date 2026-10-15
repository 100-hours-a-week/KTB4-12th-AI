"""changes — 이전 · 이번 프로필 비교(profile_change 로그, 10-15 C안). 번호 · 개수 · 축만 남고 글은 없다."""

from profiling import changes
from profiling.types import DislikedCategory, ProfileTag, RecipientProfile, ReviewRecord


def _rv(pid: int, rating: int, h: str) -> ReviewRecord:
    return ReviewRecord(product_id=pid, rating=rating, review_hash=h * 64, position=0)


def _prof(sv=1, pref="a", reviews=(), dis=(), tags=(), pool=()) -> RecipientProfile:
    return RecipientProfile(recipient_user_id=1, source_version=sv, gift_preference_hash=pref * 64 if pref else None,
                            reviews=list(reviews), disliked_categories=[DislikedCategory(category_id=c, category_name=f"c{c}") for c in dis],
                            preferred_tags=[ProfileTag(axis=a, tag=t, source="preference", ref="pref") for a, t in tags],
                            recommended_product_ids=list(pool))


def test_first_profile() -> None:
    s = changes.summarize(None, _prof(reviews=[_rv(7, 5, "a")], dis=[3], tags=[("브랜드", "소니")], pool=[1, 2]))
    assert s.first and s.input_changed and s.pref == "new" and s.reviews_added == [7] and s.dislikes_added == [3]
    assert s.tags_added == {"브랜드": 1} and s.pool_new == 2
    assert changes.log_fields(s).startswith("first=yes input_changed=yes pref=new")


def test_update_diff() -> None:
    prev = _prof(sv=1, pref="a", reviews=[_rv(7, 5, "a"), _rv(8, 5, "b"), _rv(9, 4, "c")], dis=[3],
                 tags=[("브랜드", "소니"), ("카테고리", "향수")], pool=[1, 2, 3])
    new = _prof(sv=2, pref="b", reviews=[_rv(7, 1, "d"), _rv(9, 4, "e"), _rv(10, 5, "f")], dis=[3, 4],
                tags=[("카테고리", "향수"), ("IP", "춘식이")], pool=[2, 3, 4])
    s = changes.summarize(prev, new)
    assert not s.first and not s.stale and s.prev_version == 1 and s.pref == "changed"
    assert (s.reviews_added, s.reviews_removed, s.reviews_changed, s.rating_changed) == ([10], [8], [7, 9], [7])
    assert (s.dislikes_added, s.dislikes_removed) == ([4], [])
    assert s.tags_added == {"IP": 1} and s.tags_removed == {"브랜드": 1} and s.pool_new == 1
    line = changes.log_fields(s)
    assert "reviews=+1/-1/~2" in line and "rating_changed=7" in line and "tags=+1/-1" in line and "pool_new=1/3" in line
    assert "소니" not in line and "춘식이" not in line and "향수" not in line          # 태그 글자는 남기지 않는다


def test_same_input_and_pref_states() -> None:
    p = _prof(sv=1, pref="a", reviews=[_rv(7, 5, "a")])
    s = changes.summarize(p, _prof(sv=2, pref="a", reviews=[_rv(7, 5, "a")]))
    assert not s.input_changed and s.pref == "same"
    assert changes.summarize(_prof(pref=None), _prof(sv=2, pref=None)).pref == "none"
    assert changes.summarize(_prof(pref="a"), _prof(sv=2, pref=None)).pref == "removed"


def test_stale_when_prev_is_newer() -> None:
    s = changes.summarize(_prof(sv=3), _prof(sv=2))
    assert s.stale and "stale=yes" in changes.log_fields(s)
