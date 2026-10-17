"""임베딩 어댑터 — ports.EmbeddingIndex 구현. 상품 벡터는 검색기(product-search)가 만든 것을 쓰고, 질의는 같은 모델로 인코딩한다.

  상품 벡터  두 곳에서 읽는다(설정 EMBEDDING_SOURCE).
             db(기본, 10-16) — ai_catalog.products.embedding(pgvector, 0009). 적재는 tools/catalog/load_catalog --embeddings.
                              태그와의 유사도는 DB 가 pgvector 내적 연산자(<#>)로 상품 전체에 대해 계산해 돌려준다.
             file           — product-search 가 만든 manifest.json · vectors.f32 를 직접(Jina v5 nano retrieval q4f16, 768차원).
             파일 위치는 두 형식(10-15): 옛 형식 `data/` 바로 아래 · 새 형식 `data/CURRENT`(세대 이름) → `data/generations/<이름>/`
             (검색기 search/snapshots.active_data_dir 와 같은 규칙)
  질의       product-search/embedding/service.mjs(Node, onnxruntime) 를 한 번 띄워 한 줄씩 — product-search Encoder 와 같은 규약('Query: ')
             (QueryEncoder — 어느 쪽 벡터든 같은 인코더. 모델 해시가 벡터의 모델과 다르면 켜지 않는다)
  번호 연결  옛 형식: manifest.product_ids = 수집처 ID → ai_catalog.products.source_product_id 로 찾아 products.id(BE 번호).
             새 형식(manifest 에 source_product_ids 가 있음): product_ids 가 이미 **BE 상품 번호**(BE export productId) — 그대로 쓰되
             AI 카탈로그에 있는 번호만. **맞은 상품이 MIN_MATCH_RATIO 미만이면 켜지 않는다**(조용히 0 벡터로 돌지 않게 — 10-15:
             새 형식을 옛 방식으로 읽으면 매칭 0 → 모든 상품 벡터 0 인데 오류가 없었다). DB 적재도 같은 규칙으로 검사한다.

`version` = 모델 이름 + 모델 해시 앞 8 + 벡터 해시 앞 8 — 같은 입력 = 같은 결과를 지키려면 실행 기록에 남긴다.
설정 EMBEDDING_DIR(질의 인코더가 있는 product-search 폴더)이 없으면 꺼져 있다.
numpy 는 이 모듈과 selection 의 임베딩 경로에서만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import re
import select
import subprocess
import threading
from pathlib import Path

import numpy as np
import sqlalchemy as sa

MIN_MATCH_RATIO = 0.9     # AI 카탈로그 상품 중 벡터가 있어야 하는 비율 — 밑돌면 켜지 않는다(번호 체계 · 세대가 어긋난 것)


def active_data_dir(root: Path) -> Path:
    """벡터가 든 폴더 — `data/CURRENT` 가 있으면 그 세대(`data/generations/<이름>`), 없으면 `data/`(옛 형식)."""
    data = root / "data"
    pointer = data / "CURRENT"
    if not pointer.exists():
        return data
    name = pointer.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise RuntimeError(f"data/CURRENT 의 세대 이름이 이상함: {name!r}")
    path = data / "generations" / name
    if not path.is_dir():
        raise RuntimeError(f"data/CURRENT 가 가리키는 세대 폴더가 없음: {path}")
    return path


def row_index(manifest: dict, source_to_backend: dict[str, int], catalog_backend_ids: set[int]) -> dict[int, int]:
    """BE 상품 번호 → 벡터 행 번호. 새 형식(source_product_ids 있음)은 product_ids 가 BE 번호, 옛 형식은 수집처 ID."""
    out: dict[int, int] = {}
    if "source_product_ids" in manifest:
        for i, pid in enumerate(manifest["product_ids"]):
            try:
                bid = int(pid)
            except (TypeError, ValueError):
                continue
            if bid in catalog_backend_ids:
                out[bid] = i
    else:
        for i, sid in enumerate(manifest["product_ids"]):
            if sid in source_to_backend:
                out[source_to_backend[sid]] = i
    return out


MODEL_FILE = "embedding/models/model_q4f16.onnx_data"
DIM_DB = 768              # products.embedding 의 차원(0009)


def parse_vector_files(manifest_bytes: bytes, raw_bytes: bytes) -> tuple[dict, np.ndarray]:
    """manifest.json · vectors.f32 의 내용 → (manifest, (n, d) float32). 벡터 해시가 manifest 와 다르면 실패."""
    manifest = json.loads(manifest_bytes)
    if hashlib.sha256(raw_bytes).hexdigest() != manifest["vectors_sha256"]:
        raise RuntimeError("vectors.f32 해시가 manifest 와 다름 — 검색기에서 벡터를 다시 만드세요")
    return manifest, np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, manifest["dimensions"])


def read_vector_files(data: Path) -> tuple[dict, np.ndarray]:
    """폴더의 manifest.json · vectors.f32 → parse_vector_files."""
    return parse_vector_files((data / "manifest.json").read_bytes(), (data / "vectors.f32").read_bytes())


def normalize(raw: np.ndarray) -> np.ndarray:
    """L2 정규화(float32) — 파일 방식 · DB 적재가 같은 함수를 써서 같은 값이 나온다."""
    return raw / np.linalg.norm(raw, axis=1, keepdims=True)


def version_of(model: str, model_sha256: str, vectors_sha256: str) -> str:
    return f"{model.split('/')[-1]}:{model_sha256[:8]}:{vectors_sha256[:8]}"


def _check_match(matched: int, total: int, where: str) -> None:
    if not total:
        raise RuntimeError("AI 카탈로그에 상품이 없어 벡터를 이을 수 없음 — 카탈로그를 적재한 뒤 앱을 다시 켠다")
    if matched < MIN_MATCH_RATIO * total:
        raise RuntimeError(f"상품 벡터 매칭 부족 {matched}/{total} — 벡터의 번호 체계 · 세대가 AI 카탈로그와 다름({where})")


def model_sha256(root: Path) -> str:
    """질의 인코더 모델 파일의 해시. 검색기의 model.lock.json 에 적힌 값을 쓴다 — 모델은 내려받을 때(download_model.py ·
    이미지 빌드) 이 값으로 이미 검증됐다. 기동 때마다 134MB 를 다시 해시하면 CPU 0.5 에서 25초가 걸렸다(10-16 실측).
    잠금 파일이 없으면 파일을 직접 해시한다. 모델 파일이 없으면 실패(인코더를 켤 수 없다)."""
    model = root / MODEL_FILE
    if not model.is_file():
        raise RuntimeError(f"질의 인코더 모델 파일이 없음: {MODEL_FILE} — product-search 에서 tools/download_model.py 먼저")
    lock = root / "embedding" / "model.lock.json"
    if lock.is_file():
        for f in json.loads(lock.read_text()).get("files", []):
            if f.get("path") == Path(MODEL_FILE).name and f.get("sha256"):
                return f["sha256"]
    h = hashlib.sha256()
    with model.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class QueryEncoder:
    """질의 문장 → 벡터. product-search 의 Node 인코더를 한 번 띄워 한 줄씩(워커 슬롯 여럿이 같은 프로세스를 쓴다)."""

    def __init__(self, root: Path) -> None:
        self._dir = root / "embedding"
        self.model_sha256 = model_sha256(root)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            vecs = []
            for t in texts:
                self._proc.stdin.write(json.dumps({"text": t}, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                if not select.select([self._proc.stdout], [], [], 15)[0]:
                    self.close()
                    raise RuntimeError("임베딩 응답 시간 초과")
                v = np.asarray(json.loads(self._proc.stdout.readline())["vector"], dtype=np.float32)
                vecs.append(v / np.linalg.norm(v))
            return np.stack(vecs)

    def _start(self) -> None:
        self._proc = subprocess.Popen(["node", "service.mjs"], cwd=self._dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if not select.select([self._proc.stdout], [], [], 120)[0] or not json.loads(self._proc.stdout.readline()).get("ready"):
            self.close()
            raise RuntimeError("임베딩 실행기 준비 실패 — product-search 에서 npm ci · tools/download_model.py 먼저")

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


class _VectorIndex:
    """상품 번호 → 정규화 벡터 + 질의 인코더. 두 구현(파일 · DB)이 이 모양을 공유한다."""

    source = ""
    _row: dict[int, int]
    _V: np.ndarray
    _encoder: QueryEncoder

    def product_vectors(self, product_ids: list[int]) -> np.ndarray:
        """BE 번호 → 벡터. 벡터가 없는 상품(벡터를 만든 뒤 들어온 상품)은 0 벡터 — 유사도 0 으로 후보에 거의 안 든다."""
        out = np.zeros((len(product_ids), self._V.shape[1]), dtype=np.float32)
        for k, pid in enumerate(product_ids):
            i = self._row.get(pid)
            if i is not None:
                out[k] = self._V[i]
        return out

    def similarities(self, product_ids: list[int], queries: np.ndarray) -> np.ndarray:
        """상품 × 질의 코사인(정규화 벡터의 내적) — (len(product_ids), len(queries)). 벡터 없는 상품은 0."""
        return self.product_vectors(product_ids) @ queries.T

    def encode(self, texts: list[str]) -> np.ndarray:
        return self._encoder.encode(texts)

    def close(self) -> None:
        self._encoder.close()


class ProductSearchEmbedding(_VectorIndex):
    """상품 벡터를 product-search 폴더의 파일에서 읽는다(EMBEDDING_SOURCE=file)."""

    source = "file"

    def __init__(self, root: Path, engine: sa.Engine) -> None:
        data = active_data_dir(root)
        manifest, raw = read_vector_files(data)
        self._encoder = QueryEncoder(root)
        if self._encoder.model_sha256 != manifest["model_sha256"]:
            raise RuntimeError("질의 모델과 상품 벡터의 모델이 다름(query_encoder_catalog_mismatch)")
        with engine.connect() as c:
            rows = c.execute(sa.text("select id, source_product_id from ai_catalog.products")).all()   # id = BE 번호(0008)
        back = {sid: pid for pid, sid in rows if sid is not None}          # 옛 형식 연결용: 수집처 ID → BE 번호
        catalog_ids = {pid for pid, _ in rows}
        self._row = row_index(manifest, back, catalog_ids)
        self.matched, self.total = len(self._row), len(catalog_ids)
        _check_match(self.matched, self.total, f"형식 {'새' if 'source_product_ids' in manifest else '옛'}, 폴더 {data}")
        self._V = normalize(raw)
        self.version = version_of(manifest["model"], self._encoder.model_sha256, manifest["vectors_sha256"])


def vector_literal(v) -> str:
    """numpy 벡터 → pgvector 글자 '[a,b,…]'. float32 값을 그대로 옮긴다(repr 은 가장 짧은 정확한 표기)."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


class DbEmbedding(_VectorIndex):
    """상품 벡터를 AI DB 에서 읽는다(EMBEDDING_SOURCE=db, 기본). 벡터는 ai_catalog.products.embedding(pgvector, 0009),
    유사도는 DB 가 pgvector 내적(<#>)으로 계산한다 — 상품 전체 × 태그를 한 번의 질의로. MMR 에 쓸 후보 벡터만 따로 읽는다.
    질의 인코더는 root 의 Node(모델 해시가 활성 카탈로그 판의 벡터 모델과 같아야 켜진다)."""

    source = "db"

    def __init__(self, root: Path, engine: sa.Engine) -> None:
        self._engine = engine
        with engine.connect() as c:
            v = c.execute(sa.text("""select id, embedding_model, embedding_model_sha256, embedding_vectors_sha256
                                     from ai_catalog.catalog_versions where is_active""")).first()
            if v is None or v.embedding_model is None:
                raise RuntimeError("활성 카탈로그 판에 상품 벡터가 없음 — load_catalog --embeddings 로 적재한 뒤 앱을 다시 켠다")
            self.matched, self.total = c.execute(sa.text(
                "select count(embedding), count(*) from ai_catalog.products")).one()
        self._encoder = QueryEncoder(root)
        if self._encoder.model_sha256 != v.embedding_model_sha256:
            raise RuntimeError("질의 모델과 DB 상품 벡터의 모델이 다름(query_encoder_catalog_mismatch)")
        _check_match(self.matched, self.total, f"DB 카탈로그 판 {v.id}")
        self.version = version_of(v.embedding_model, v.embedding_model_sha256, v.embedding_vectors_sha256)

    def similarities(self, product_ids: list[int], queries: np.ndarray) -> np.ndarray:
        out = np.zeros((len(product_ids), len(queries)), dtype=np.float32)
        if not product_ids:
            return out
        pos = {pid: k for k, pid in enumerate(product_ids)}
        with self._engine.connect() as c:
            rows = c.execute(sa.text("""
                select p.id, q.k - 1, -(p.embedding <#> q.v)
                  from ai_catalog.products p
                  cross join unnest(cast(:qs as vector[])) with ordinality as q(v, k)
                 where p.id = any(:ids) and p.embedding is not null"""),
                {"qs": [vector_literal(q) for q in queries], "ids": list(pos)}).all()
        for pid, j, sim in rows:
            out[pos[pid], j] = sim
        return out

    def product_vectors(self, product_ids: list[int]) -> np.ndarray:
        out = np.zeros((len(product_ids), DIM_DB), dtype=np.float32)
        if not product_ids:
            return out
        pos = {pid: k for k, pid in enumerate(product_ids)}
        with self._engine.connect() as c:
            rows = c.execute(sa.text("""select id, embedding::real[] from ai_catalog.products
                                        where id = any(:ids) and embedding is not null"""), {"ids": list(pos)}).all()
        for pid, vec in rows:
            out[pos[pid]] = vec
        return out
