"""상품 임베딩 벡터 — products.embedding(pgvector, 0009) 적재(load_catalog --embeddings)와 읽기(embedding.DbEmbedding).

진짜 PostgreSQL 에 활성 카탈로그가 있어야 돈다(없으면 skip). 실제 적재분을 건드리므로 **벡터 열과 판 정보는 시험 뒤 원래대로 되돌린다**.
가짜 product-search 폴더(manifest · vectors.f32 · 모델 파일)를 tmp 에 만든다 — Node 인코더는 띄우지 않는다(encode 를 부르지 않음).
"""

import hashlib
import json
import os

import numpy as np
import pytest
import sqlalchemy as sa

from profiling.embedding import MODEL_FILE, DbEmbedding, normalize
from profiling.settings import get_settings
from tools.catalog.load_catalog import (
    PackageError,
    has_active_embeddings,
    load_embeddings,
    read_embeddings,
)

SCHEMA = "ai_catalog"
DIM = 768
ROLE = os.environ.get("PROFILING_APP_DB_ROLE") or "ai_app"     # 마이그레이션과 같은 규칙


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    with eng.connect() as c:
        if not c.execute(sa.text(f"select count(*) from {SCHEMA}.catalog_versions where is_active")).scalar_one():
            pytest.skip("활성 카탈로그가 없음 — load_catalog.py 로 적재 후 실행")
    yield eng
    eng.dispose()


@pytest.fixture
def restore(engine):
    """시험 전 벡터 열 · 활성 판의 벡터 정보를 떠 두고, 끝나면 그대로 되돌린다."""
    with engine.connect() as c:
        vecs = c.execute(sa.text(f"select id, embedding::text from {SCHEMA}.products where embedding is not null")).all()
        info = c.execute(sa.text(f"""select id, embedding_model, embedding_model_sha256, embedding_vectors_sha256, embedding_loaded_at
                                     from {SCHEMA}.catalog_versions where is_active""")).mappings().one()
    yield
    with engine.begin() as c:
        c.execute(sa.text(f"update {SCHEMA}.products set embedding = null where embedding is not null"))
        if vecs:
            c.execute(sa.text(f"update {SCHEMA}.products set embedding = cast(:v as vector) where id = :id"),
                      [{"id": i, "v": v} for i, v in vecs])
        c.execute(sa.text(f"""update {SCHEMA}.catalog_versions set embedding_model = :embedding_model,
                                  embedding_model_sha256 = :embedding_model_sha256, embedding_vectors_sha256 = :embedding_vectors_sha256,
                                  embedding_loaded_at = :embedding_loaded_at where id = :id"""), dict(info))


def _fake_root(tmp_path, ids, *, dim=DIM, model=b"fake-model", seed=0):
    """새 형식(product_ids = BE 번호) 가짜 product-search 폴더 → (root, 원본 벡터)."""
    raw = np.random.default_rng(seed).standard_normal((len(ids), dim)).astype(np.float32)
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "vectors.f32").write_bytes(raw.tobytes())
    (tmp_path / MODEL_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / MODEL_FILE).write_bytes(model)
    manifest = {"model": "test/fake-embedding", "dimensions": dim, "model_sha256": hashlib.sha256(model).hexdigest(),
                "vectors_sha256": hashlib.sha256(raw.tobytes()).hexdigest(), "product_ids": [str(i) for i in ids],
                "source_product_ids": [f"src-{i}" for i in ids]}
    (data / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path, raw


def _ids(engine):
    with engine.connect() as c:
        return [r[0] for r in c.execute(sa.text(f"select id from {SCHEMA}.products order by id"))]


def test_load_then_read_back_same_vectors_and_similarities(engine, restore, tmp_path) -> None:
    ids = _ids(engine)
    root, raw = _fake_root(tmp_path, ids)
    manifest, got_raw = read_embeddings(str(root))                 # product-search 폴더 → data/ 규칙
    got = load_embeddings(engine, manifest, got_raw)
    assert (got["matched"], got["total"], got["format"]) == (len(ids), len(ids), "new")
    assert has_active_embeddings(engine)

    emb = DbEmbedding(root, engine)
    assert (emb.source, emb.matched, emb.total) == ("db", len(ids), len(ids))
    pick = ids[:: max(len(ids) // 50, 1)]
    want = normalize(raw)[[ids.index(i) for i in pick]]
    assert np.array_equal(emb.product_vectors(pick), want)          # float32 그대로 왕복
    q = normalize(np.random.default_rng(1).standard_normal((3, DIM)).astype(np.float32))
    assert np.allclose(emb.similarities(pick, q), want @ q.T, atol=1e-5)   # pgvector 내적 = numpy 내적
    assert emb.similarities([], q).shape == (0, 3)


def test_wrong_hash_dimension_or_numbering_is_refused(engine, restore, tmp_path) -> None:
    ids = _ids(engine)
    root, _ = _fake_root(tmp_path / "a", ids)
    (root / "data" / "vectors.f32").write_bytes(b"\0" * 8)          # 해시가 manifest 와 다름
    with pytest.raises(PackageError, match="해시"):
        read_embeddings(str(root))

    root, raw = _fake_root(tmp_path / "b", ids, dim=16)
    manifest, raw = read_embeddings(str(root / "data"))             # manifest 가 든 폴더도 받는다
    with pytest.raises(PackageError, match="차원"):
        load_embeddings(engine, manifest, raw)

    root, raw = _fake_root(tmp_path / "c", [i + 10_000_000 for i in ids])   # 카탈로그에 없는 번호
    manifest, raw = read_embeddings(str(root))
    with pytest.raises(PackageError, match="매칭 부족"):
        load_embeddings(engine, manifest, raw)


def test_app_refuses_vectors_from_another_model(engine, restore, tmp_path) -> None:
    ids = _ids(engine)
    root, _ = _fake_root(tmp_path, ids, model=b"model-A")
    load_embeddings(engine, *read_embeddings(str(root)))
    (root / MODEL_FILE).write_bytes(b"model-B")                     # 앱의 질의 인코더가 다른 모델
    with pytest.raises(RuntimeError, match="모델이 다름"):
        DbEmbedding(root, engine)


def test_app_role_reads_vectors_but_cannot_write(engine) -> None:
    with engine.connect() as c:
        if not c.execute(sa.text("select 1 from pg_roles where rolname = :r"), {"r": ROLE}).scalar():
            pytest.skip("앱 계정 없음 — alembic upgrade head 전")
        priv = c.execute(sa.text("""select has_column_privilege(:r, 'ai_catalog.products', 'embedding', 'SELECT'),
                                           has_column_privilege(:r, 'ai_catalog.products', 'embedding', 'UPDATE'),
                                           has_column_privilege(:r, 'ai_catalog.catalog_versions', 'embedding_model', 'SELECT')"""),
                         {"r": ROLE}).one()
    assert tuple(priv) == (True, False, True)
