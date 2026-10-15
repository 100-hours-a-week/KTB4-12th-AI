"""임베딩 어댑터 — ports.EmbeddingIndex 구현. 상품 벡터는 검색기(product-search)가 만든 것을 읽고, 질의는 같은 모델로 인코딩한다.

  상품 벡터  product-search/data/vectors.f32 · manifest.json (Jina v5 nano retrieval q4f16, 768차원, 4,231개) — 읽기만
  질의       product-search/embedding/service.mjs(Node, onnxruntime) 를 한 번 띄워 한 줄씩 — product-search Encoder 와 같은 규약('Query: ')
  번호 연결  manifest 의 수집처 상품 ID → ai_catalog.products.source_product_id 로 찾아 products.id (BE 번호)

`version` = 모델 해시 앞 8 + 벡터 해시 앞 8 — 같은 입력 = 같은 결과를 지키려면 실행 기록에 남긴다.
모델 라이선스 CC BY-NC 4.0(비상업) — 서비스 반영 전 확인 필요. 그래서 설정 EMBEDDING_DIR 이 없으면 꺼져 있다.
numpy 는 이 모듈과 selection 의 임베딩 경로에서만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import select
import subprocess
import threading
from pathlib import Path

import numpy as np
import sqlalchemy as sa


class ProductSearchEmbedding:
    def __init__(self, root: Path, engine: sa.Engine) -> None:
        data = root / "data"
        manifest = json.loads((data / "manifest.json").read_text())
        raw = np.fromfile(data / "vectors.f32", dtype=np.float32).reshape(-1, manifest["dimensions"])
        if hashlib.sha256((data / "vectors.f32").read_bytes()).hexdigest() != manifest["vectors_sha256"]:
            raise RuntimeError("vectors.f32 해시가 manifest 와 다름 — product-search/embedding/build.mjs 를 다시 돌리세요")
        model_sha = hashlib.sha256((root / "embedding/models/model_q4f16.onnx_data").read_bytes()).hexdigest()
        if model_sha != manifest["model_sha256"]:
            raise RuntimeError("질의 모델과 상품 벡터의 모델이 다름(query_encoder_catalog_mismatch)")
        with engine.connect() as c:
            back = dict(c.execute(sa.text("select source_product_id, id from ai_catalog.products "
                                          "where source_product_id is not null")).all())
        self._row = {back[sid]: i for i, sid in enumerate(manifest["product_ids"]) if sid in back}
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
