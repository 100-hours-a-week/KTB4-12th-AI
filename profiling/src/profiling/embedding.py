"""임베딩 어댑터 — ports.EmbeddingIndex 구현. 상품 벡터는 검색기(product-search)가 만든 것을 읽고, 질의는 같은 모델로 인코딩한다.

  상품 벡터  product-search 가 만든 manifest.json · vectors.f32 (Jina v5 nano retrieval q4f16, 768차원) — 읽기만.
             위치는 두 형식(10-15): 옛 형식 `data/` 바로 아래 · 새 형식 `data/CURRENT`(세대 이름) → `data/generations/<이름>/`
             (검색기 search/snapshots.active_data_dir 와 같은 규칙)
  질의       product-search/embedding/service.mjs(Node, onnxruntime) 를 한 번 띄워 한 줄씩 — product-search Encoder 와 같은 규약('Query: ')
  번호 연결  옛 형식: manifest.product_ids = 수집처 ID → ai_catalog.products.source_product_id 로 찾아 products.id(BE 번호).
             새 형식(manifest 에 source_product_ids 가 있음): product_ids 가 이미 **BE 상품 번호**(BE export productId) — 그대로 쓰되
             AI 카탈로그에 있는 번호만. **맞은 상품이 MIN_MATCH_RATIO 미만이면 기동 때 실패**(조용히 0 벡터로 돌지 않게 — 10-15:
             새 형식을 옛 방식으로 읽으면 매칭 0 → 모든 상품 벡터 0 인데 오류가 없었다)

`version` = 모델 해시 앞 8 + 벡터 해시 앞 8 — 같은 입력 = 같은 결과를 지키려면 실행 기록에 남긴다.
모델 라이선스 CC BY-NC 4.0(비상업) — 서비스 반영 전 확인 필요. 그래서 설정 EMBEDDING_DIR 이 없으면 꺼져 있다.
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


class ProductSearchEmbedding:
    def __init__(self, root: Path, engine: sa.Engine) -> None:
        data = active_data_dir(root)
        manifest = json.loads((data / "manifest.json").read_text())
        raw = np.fromfile(data / "vectors.f32", dtype=np.float32).reshape(-1, manifest["dimensions"])
        if hashlib.sha256((data / "vectors.f32").read_bytes()).hexdigest() != manifest["vectors_sha256"]:
            raise RuntimeError("vectors.f32 해시가 manifest 와 다름 — 검색기에서 벡터를 다시 만드세요")
        model_sha = hashlib.sha256((root / "embedding/models/model_q4f16.onnx_data").read_bytes()).hexdigest()
        if model_sha != manifest["model_sha256"]:
            raise RuntimeError("질의 모델과 상품 벡터의 모델이 다름(query_encoder_catalog_mismatch)")
        with engine.connect() as c:
            rows = c.execute(sa.text("select id, source_product_id from ai_catalog.products")).all()   # id = BE 번호(0008)
        back = {sid: pid for pid, sid in rows if sid is not None}          # 옛 형식 연결용: 수집처 ID → BE 번호
        catalog_ids = {pid for pid, _ in rows}
        self._row = row_index(manifest, back, catalog_ids)
        self.matched, self.total = len(self._row), len(catalog_ids)
        if not self.total:
            raise RuntimeError("AI 카탈로그에 상품이 없어 벡터를 이을 수 없음 — 카탈로그를 적재한 뒤 앱을 다시 켠다")
        if self.matched < MIN_MATCH_RATIO * self.total:
            raise RuntimeError(f"상품 벡터 매칭 부족 {self.matched}/{self.total} — 벡터의 번호 체계 · 세대가 AI 카탈로그와 다름"
                               f"(형식 {'새' if 'source_product_ids' in manifest else '옛'}, 폴더 {data})")
        self._V = raw / np.linalg.norm(raw, axis=1, keepdims=True)
        self._dim = manifest["dimensions"]
        self.version = f"{manifest['model'].split('/')[-1]}:{model_sha[:8]}:{manifest['vectors_sha256'][:8]}"
        self._dir = root / "embedding"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()                 # 워커 슬롯 여럿이 같은 Node 프로세스를 쓴다 — 한 줄씩

    def product_vectors(self, product_ids: list[int]) -> np.ndarray:
        """BE 번호 → 벡터. 벡터가 없는 상품(검색기 스냅샷 뒤에 들어온 상품)은 0 벡터 — 유사도 0 으로 후보에 거의 안 든다."""
        out = np.zeros((len(product_ids), self._dim), dtype=np.float32)
        for k, pid in enumerate(product_ids):
            i = self._row.get(pid)
            if i is not None:
                out[k] = self._V[i]
        return out

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
