"""embedding — 검색기 벡터의 두 형식(옛 `data/` · 새 `data/CURRENT` → 세대) 읽기와 상품 번호 연결 · 매칭 부족이면 켜지 않기(10-15).

새 형식은 manifest.product_ids 가 BE 상품 번호다. 옛 방식(수집처 ID)으로 읽으면 매칭 0 → 모든 벡터 0 인데 오류가 없었다 — 그걸 막는다.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from profiling import embedding


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return _Result(self._rows)


class _Engine:
    """ai_catalog.products 의 (id, source_product_id) 만 돌려주는 가짜 — id 가 BE 번호(0008)."""

    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _Conn(self._rows)


CATALOG = [(101, "KAKAO_GIFT:1"), (102, "KAKAO_GIFT:2"), (103, "KAKAO_GIFT:3")]


def _write(root: Path, data_dir: Path, product_ids, source_ids=None) -> None:
    (root / "embedding/models").mkdir(parents=True)
    model = root / "embedding/models/model_q4f16.onnx_data"
    model.write_bytes(b"model")
    data_dir.mkdir(parents=True, exist_ok=True)
    vec = np.arange(len(product_ids) * 4, dtype=np.float32) + 1
    (data_dir / "vectors.f32").write_bytes(vec.tobytes())
    manifest = {"model": "jinaai/test", "dimensions": 4, "product_ids": product_ids,
                "vectors_sha256": hashlib.sha256(vec.tobytes()).hexdigest(),
                "model_sha256": hashlib.sha256(b"model").hexdigest()}
    if source_ids is not None:
        manifest["source_product_ids"] = source_ids
    (data_dir / "manifest.json").write_text(json.dumps(manifest))


def test_old_format_reads_data_dir_and_maps_source_ids(tmp_path) -> None:
    _write(tmp_path, tmp_path / "data", ["KAKAO_GIFT:1", "KAKAO_GIFT:2", "KAKAO_GIFT:3"])
    emb = embedding.ProductSearchEmbedding(tmp_path, _Engine(CATALOG))
    assert (emb.matched, emb.total) == (3, 3)
    assert emb.product_vectors([102]).any()


def test_new_format_follows_current_and_uses_backend_ids(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/CURRENT").write_text("g20261015\n")
    _write(tmp_path, tmp_path / "data/generations/g20261015", [101, 102, 103],
           source_ids=["KAKAO_GIFT:1", "KAKAO_GIFT:2", "KAKAO_GIFT:3"])
    emb = embedding.ProductSearchEmbedding(tmp_path, _Engine(CATALOG))
    assert (emb.matched, emb.total) == (3, 3)
    assert emb.product_vectors([103]).any() and not emb.product_vectors([999]).any()


def test_refuses_when_too_few_products_match(tmp_path) -> None:
    """번호 체계가 어긋나면(새 형식인데 표시가 없어 옛 방식으로 읽음) 켜지 않는다 — 조용히 0 벡터로 돌지 않게."""
    _write(tmp_path, tmp_path / "data", [101, 102, 103])                  # BE 번호인데 source_product_ids 표시가 없음
    with pytest.raises(RuntimeError, match="매칭 부족"):
        embedding.ProductSearchEmbedding(tmp_path, _Engine(CATALOG))


def test_bad_current_pointer_is_an_error(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/CURRENT").write_text("../etc")
    with pytest.raises(RuntimeError, match="세대 이름"):
        embedding.active_data_dir(tmp_path)
    (tmp_path / "data/CURRENT").write_text("missing")
    with pytest.raises(RuntimeError, match="세대 폴더가 없음"):
        embedding.active_data_dir(tmp_path)


def test_refuses_when_catalog_empty(tmp_path) -> None:
    """기동 때 AI 카탈로그가 비어 있으면(첫 배포 순서가 어긋남) 0/0 으로 켜지지 않는다."""
    _write(tmp_path, tmp_path / "data", ["KAKAO_GIFT:1"])
    with pytest.raises(RuntimeError, match="카탈로그를 적재한 뒤"):
        embedding.ProductSearchEmbedding(tmp_path, _Engine([]))
