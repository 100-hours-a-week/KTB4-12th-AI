"""0009 — 상품 임베딩 벡터를 pgvector 열로 (products.embedding · catalog_versions 의 벡터 정보)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16

왜: 추천 30개의 임베딩 단계(selection.D 의 4)는 상품 벡터가 필요하다. 지금까지는 검색기(product-search)가 만든 파일
(manifest.json · vectors.f32)을 프로파일링이 직접 읽어서, 그 폴더가 없는 서버 컨테이너에서는 임베딩이 꺼졌다.
0001 에서 켜 둔 vector 확장으로 벡터를 상품 표에 두고, 태그와의 유사도는 pgvector 연산자(<#>)로 DB 가 계산한다.
표는 늘리지 않는다 — 기존 표에 열만 더한다(10-16 결정).

  products.embedding           vector(768), L2 정규화(내적 = 코사인). 벡터가 없는 상품은 NULL(유사도 0 으로 본다)
  catalog_versions.embedding_* 그 카탈로그 판에 넣은 벡터의 모델 이름 · 모델 해시 · 벡터 파일 해시 · 적재 시각.
                               앱의 질의 인코더 모델 해시와 다르면 임베딩을 켜지 않는다. 카탈로그를 새 판으로 다시 넣으면
                               새 판에는 값이 없으므로 벡터도 다시 넣어야 켜진다(검증 없이 옛 벡터를 쓰지 않게).

권한: 0007 로 앱 전용 계정은 ai_catalog 표를 읽기만 한다 — 새 열도 같다.
되돌리기(downgrade): 열을 지운다(벡터는 적재 도구로 다시 넣을 수 있는 자료).
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SCHEMA = "ai_catalog"
DIM = 768          # 검색기 모델(Jina v5 nano retrieval)의 차원 — 모델을 바꾸면 새 마이그레이션

_COMMENTS = (
    (f"{SCHEMA}.products.embedding", "L2 정규화한 상품 벡터(pgvector) — 내적 = 코사인. 없으면 NULL"),
    (f"{SCHEMA}.catalog_versions.embedding_model", "이 판에 넣은 상품 벡터의 모델 이름(manifest.model). 없으면 벡터 없음"),
    (f"{SCHEMA}.catalog_versions.embedding_model_sha256", "모델 파일 해시 — 앱의 질의 인코더 모델과 같아야 임베딩이 켜진다"),
    (f"{SCHEMA}.catalog_versions.embedding_vectors_sha256", "벡터 파일(vectors.f32) 해시 — 어느 벡터를 넣었나"),
    (f"{SCHEMA}.catalog_versions.embedding_loaded_at", "벡터 적재 시각"),
)


def upgrade() -> None:
    op.execute(f"alter table {SCHEMA}.products add column embedding vector({DIM})")
    op.execute(f"""alter table {SCHEMA}.catalog_versions
                   add column embedding_model text,
                   add column embedding_model_sha256 char(64),
                   add column embedding_vectors_sha256 char(64),
                   add column embedding_loaded_at timestamptz""")
    for col, text in _COMMENTS:
        op.execute("comment on column {} is '{}'".format(col, text.replace("'", "''")))   # COMMENT 는 바인드 변수를 못 받는다


def downgrade() -> None:
    op.execute(f"""alter table {SCHEMA}.catalog_versions
                   drop column embedding_loaded_at, drop column embedding_vectors_sha256,
                   drop column embedding_model_sha256, drop column embedding_model""")
    op.execute(f"alter table {SCHEMA}.products drop column embedding")
