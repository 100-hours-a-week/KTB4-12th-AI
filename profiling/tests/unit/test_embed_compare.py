"""tools/embed_compare.strategies — 가짜 벡터로 대체 · 결정성 · MMR 섞임 · 비선호 처리를 본다(모델 · DB 없이)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np

from tools.embed_compare.run import POOL, dup_key, strategies


def _ranked(cats: list[int], disliked: set[int] = frozenset(), names: dict[int, str] | None = None):
    """S0 순서 그대로의 가짜 rank_products 결과 — total 은 앞일수록 크다."""
    out = []
    for i, c in enumerate(cats):
        p = SimpleNamespace(productId=i + 1, categoryName=f"c{c}", categoryId=c, viewCount=1000 - i, brand="b",
                            name=(names or {}).get(i, f"상품 {i}"), updatedAt=datetime(2026, 10, 1, tzinfo=UTC))
        out.append(SimpleNamespace(product=p, popularity=0.0, bias=0.0, penalty=-2.0 if i in disliked else 0.0,
                                   total=(len(cats) - i) / len(cats) * 2 + (-2.0 if i in disliked else 0.0), disliked=i in disliked))
    return out


def _vectors(cats: list[int], dim: int = 8, seed: int = 0) -> np.ndarray:
    """같은 소분류는 거의 같은 방향 — 소분류 c 의 축 c 에 몰린 벡터."""
    rng = np.random.default_rng(seed)
    v = np.stack([np.eye(dim)[c] + 0.05 * rng.standard_normal(dim) for c in cats]).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_no_tags_falls_back_to_s0() -> None:
    cats = [i % 4 for i in range(80)]
    st = strategies(_ranked(cats), np.zeros((0, 8), dtype=np.float32), _vectors(cats), True)
    assert st["fallback"] and st["A"] == st["B"] == st["C"] == st["S0"] == list(range(POOL))


def test_deterministic_and_pool_size() -> None:
    cats = [i % 6 for i in range(200)]
    V, q = _vectors(cats), np.eye(8, dtype=np.float32)[[0, 1]]
    a, b = strategies(_ranked(cats), q, V, True), strategies(_ranked(cats), q, V, True)
    for k in ("S0", "A", "B", "C"):
        assert a[k] == b[k] and len(a[k]) == POOL and len(set(a[k])) == POOL


def test_mmr_mixes_where_b_concentrates() -> None:
    cats = [0] * 60 + [1] * 60 + [2] * 60          # 태그는 소분류 0 하나 — B 는 0 으로 몰리고 C 는 섞여야 한다
    V, q = _vectors(cats, seed=1), np.eye(8, dtype=np.float32)[[0]]
    st = strategies(_ranked(cats), q, V, True)
    n_cat = lambda pos: len({cats[i] for i in pos})
    assert all(cats[i] == 0 for i in st["A"]) and n_cat(st["C"]) >= n_cat(st["B"])


def test_penalty_mode_puts_disliked_last_in_a() -> None:
    cats = [0] * 40 + [1] * 40
    disliked = set(range(30))                       # 소분류 0 의 앞 30개가 비선호 — 후보 50 중 비선호 아닌 것은 20개
    st = strategies(_ranked(cats, disliked), np.eye(8, dtype=np.float32)[[0]], _vectors(cats), True)
    flags = [i in disliked for i in st["A"]]
    assert any(flags) and flags == sorted(flags)      # 비선호는 모두 비선호 아닌 것 뒤에


def test_d_excludes_disliked_when_enough() -> None:
    cats = [0] * 40 + [1] * 40
    disliked = set(range(40))                       # 태그가 가리키는 소분류 0 전체가 비선호(U09 꼴)
    st = strategies(_ranked(cats, disliked), np.eye(8, dtype=np.float32)[[0]], _vectors(cats), True, ["카테고리"])
    assert len(st["D"]) == POOL and not set(st["D"]) & disliked and st["d_filled_disliked"] == 0


def test_d_fills_with_disliked_only_when_short() -> None:
    cats = [0] * 40 + [1] * 10
    disliked = set(range(40))                       # 비선호 아닌 상품은 10개뿐 → 20개는 비선호를 감점(S0) 순서로
    st = strategies(_ranked(cats, disliked), np.eye(8, dtype=np.float32)[[1]], _vectors(cats), True, ["카테고리"])
    d = st["D"]
    assert len(d) == POOL and set(d[:10]) == set(range(40, 50)) and d[10:] == list(range(20)) and st["d_filled_disliked"] == 20


def test_d_drops_same_model_variants() -> None:
    cats = [0] * 60
    names = {i: f"SONY WH-1000XM6 헤드폰 색상{i}" for i in range(10)}   # 앞 10개는 같은 모델의 색상 차이
    st = strategies(_ranked(cats, names=names), np.eye(8, dtype=np.float32)[[0]], _vectors(cats), True, ["카테고리"])
    assert sum(i < 10 for i in st["D"]) == 1
    assert dup_key(SimpleNamespace(name="[공식] SONY WH-1000XM6 올리브", brand="소니")) == ("소니", "WH-1000XM6")
    assert dup_key(SimpleNamespace(name="부드러운 고구마 케이크", brand="투썸")) is None


def test_d_caps_one_subcategory() -> None:
    from tools.embed_compare import run as ec

    cats = [0] * 60 + [1] * 30 + [2] * 30          # 태그는 소분류 0 하나 — 상한 때문에 0 은 최대 D_CAT_CAP 개, 나머지는 S0 순서로 채움
    st = strategies(_ranked(cats), np.eye(8, dtype=np.float32)[[0]], _vectors(cats), True, ["카테고리"])
    assert len(st["D"]) == POOL and sum(cats[i] == 0 for i in st["D"]) == ec.D_CAT_CAP
