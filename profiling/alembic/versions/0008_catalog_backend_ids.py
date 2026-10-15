"""0008 — ai_catalog 키를 Backend 번호로 (categories.id · parent_id, products.id · category_id)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15

왜: 0003 은 Backend 번호가 아직 없을 때 만들어 수집처 문자열(`CAT-01-02` · `KAKAO_GIFT:…`)을 PK 로 삼고 Backend 번호를
nullable 열(backend_*_id)로 붙였다. 지금은 Backend 번호가 정본이다 — 상품 시드(Backend `V10__seed_product_catalog.sql`)의
categories.id 1~67 · products.id 1~4231 이 7.6 · 7.7 · 비선호 · 별점 누적에서 쓰는 번호이고, AI 카탈로그 67/67 · 4,231/4,231 이 이미 채워져 있다.
그래서 표의 키를 Backend 와 같은 이름 · 같은 값으로 바꾼다.

  categories  PK backend_category_id → **id**(Backend categories.id) · 부모 parent_source_category_id → **parent_id**(Backend parent_id)
              source_category_id · parent_source_category_id 삭제
  products    PK backend_product_id → **id**(Backend products.id) · 소분류 source_category_id → **category_id**(Backend category_id)
              source_product_id 는 키가 아닌 일반 열로 남긴다(nullable) — 패키지 원본 추적과, 수집처 ID 로 만든 옛 형식
              상품 벡터(product-search manifest)를 잇는 데만 쓴다. Backend 와 주고받지 않는다.

데이터: 행이 있으면 그대로 옮긴다. 단 Backend 번호가 비어 있는 행이 하나라도 있으면 **멈춘다**(키를 만들 수 없다) —
load_catalog.py 로 Backend 번호를 채운 뒤 다시 돌린다.
되돌리기(downgrade): 표가 비어 있을 때만 구조를 0007 로 되돌린다. 행이 있으면 지운 수집처 카테고리 ID 를 복원할 수 없으므로 멈춘다.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = "ai_catalog"
C = f"{SCHEMA}.categories"
P = f"{SCHEMA}.products"


def _count(sql: str) -> int:
    return op.get_bind().execute(sa.text(sql)).scalar_one()


def upgrade() -> None:
    missing_c = _count(f"select count(*) from {C} where backend_category_id is null")
    missing_p = _count(f"select count(*) from {P} where backend_product_id is null")
    if missing_c or missing_p:
        raise RuntimeError(f"Backend 번호가 없는 행이 있어 키를 바꿀 수 없다 — 카테고리 {missing_c} · 상품 {missing_p}. "
                           "load_catalog.py 로 Backend 번호를 채운 뒤 다시 실행")

    # ---- 새 열 채우기 (옛 키로 잇는다) ---------------------------------------
    op.execute(f"alter table {C} add column parent_id bigint")
    op.execute(f"update {C} c set parent_id = p.backend_category_id from {C} p where p.source_category_id = c.parent_source_category_id")
    op.execute(f"alter table {P} add column category_id bigint")
    op.execute(f"update {P} p set category_id = c.backend_category_id from {C} c where c.source_category_id = p.source_category_id")

    # ---- 옛 제약 · 인덱스 걷기 -----------------------------------------------
    for t, name in ((P, "fk_products_category"), (C, "fk_categories_parent"), (C, "ck_categories_parent_by_level"),
                    (P, "products_pkey"), (C, "categories_pkey"), (P, "uq_products_backend_id"), (C, "uq_categories_backend_id"),
                    (P, "ck_products_backend_id")):
        op.execute(f"alter table {t} drop constraint {name}")
    op.execute(f"drop index {SCHEMA}.ix_products_category")
    op.execute(f"drop index {SCHEMA}.ix_products_view_count")
    op.execute(f"drop index {SCHEMA}.ix_categories_parent")

    # ---- categories: id · parent_id ------------------------------------------
    op.execute(f"alter table {C} rename column backend_category_id to id")
    op.execute(f"alter table {C} alter column id set not null")
    op.execute(f"alter table {C} add constraint categories_pkey primary key (id)")
    op.execute(f"alter table {C} add constraint ck_categories_id check (id > 0)")
    op.execute(f"alter table {C} add constraint fk_categories_parent foreign key (parent_id) references {C}(id)")
    op.execute(f"alter table {C} add constraint ck_categories_parent_by_level check ((level = 1) = (parent_id is null))")
    op.execute(f"alter table {C} drop column parent_source_category_id")
    op.execute(f"alter table {C} drop column source_category_id")
    op.execute(f"create index ix_categories_parent on {C} (parent_id)")

    # ---- products: id · category_id ------------------------------------------
    op.execute(f"alter table {P} rename column backend_product_id to id")
    op.execute(f"alter table {P} alter column id set not null")
    op.execute(f"alter table {P} add constraint products_pkey primary key (id)")
    op.execute(f"alter table {P} add constraint ck_products_id check (id > 0)")
    op.execute(f"alter table {P} alter column category_id set not null")
    op.execute(f"alter table {P} add constraint fk_products_category foreign key (category_id) references {C}(id)")
    op.execute(f"alter table {P} drop column source_category_id")
    op.execute(f"alter table {P} alter column source_product_id drop not null")
    op.execute(f"create index ix_products_category on {P} (category_id)")
    op.execute(f"create index ix_products_view_count on {P} (view_count desc, id)")

    # ---- 주석 (DB 안 설명도 지금 뜻으로) --------------------------------------
    comments = {
        f"{C}.id": "Backend categories.id (상품 시드 1~67). 대분류 1~10 · 소분류 11~67. 7.6 dislikedCategories[].categoryId 가 이 값",
        f"{C}.parent_id": "Backend categories.parent_id — 소분류만 값이 있다(대분류 번호)",
        f"{C}.product_count": "패키지가 선언한 상품 수 (적재 후 실제 수와 대조)",
        f"{P}.id": "Backend products.id (상품 시드 1~4231). 7.6 reviews[].productId · 7.7 recommendedProductIds 가 이 값",
        f"{P}.category_id": "Backend products.category_id — 소분류 번호(categories.id, level 2)",
        f"{P}.source_product_id": "패키지 원본의 수집처 상품 ID — 원본 추적 · 옛 형식 상품 벡터 연결용. 키 아님, Backend 와 주고받지 않는다",
        f"{P}.availability": "재고 3값 available · unavailable · unknown. 추천에서 빼는 것은 unavailable 뿐",
        f"{P}.view_count": "Backend products.views — 인기 백분위의 재료",
    }
    for col, text in comments.items():
        op.execute("comment on column {} is '{}'".format(col, text.replace("'", "''")))   # COMMENT 는 바인드 변수를 못 받는다


def downgrade() -> None:
    rows = _count(f"select (select count(*) from {C}) + (select count(*) from {P})")
    if rows:
        raise RuntimeError(f"ai_catalog 에 행이 {rows}개 있어 0007 로 되돌릴 수 없다(수집처 카테고리 ID 를 복원할 수 없음). "
                           "카탈로그 표를 비운 뒤 다시 실행하고, 0007 구조로 패키지를 다시 적재한다")
    for t, name in ((P, "fk_products_category"), (C, "fk_categories_parent"), (C, "ck_categories_parent_by_level"),
                    (P, "products_pkey"), (C, "categories_pkey"), (P, "ck_products_id"), (C, "ck_categories_id")):
        op.execute(f"alter table {t} drop constraint {name}")
    op.execute(f"drop index {SCHEMA}.ix_products_category")
    op.execute(f"drop index {SCHEMA}.ix_products_view_count")
    op.execute(f"drop index {SCHEMA}.ix_categories_parent")

    op.execute(f"alter table {C} add column source_category_id text not null")
    op.execute(f"alter table {C} add column parent_source_category_id text")
    op.execute(f"alter table {C} drop column parent_id")
    op.execute(f"alter table {C} rename column id to backend_category_id")
    op.execute(f"alter table {C} alter column backend_category_id drop not null")
    op.execute(f"alter table {C} add constraint categories_pkey primary key (source_category_id)")
    op.execute(f"alter table {C} add constraint uq_categories_backend_id unique (backend_category_id)")
    op.execute(f"alter table {C} add constraint fk_categories_parent foreign key (parent_source_category_id) references {C}(source_category_id)")
    op.execute(f"alter table {C} add constraint ck_categories_parent_by_level check ((level = 1) = (parent_source_category_id is null))")
    op.execute(f"create index ix_categories_parent on {C} (parent_source_category_id)")

    op.execute(f"alter table {P} alter column source_product_id set not null")
    op.execute(f"alter table {P} add column source_category_id text not null")
    op.execute(f"alter table {P} drop column category_id")
    op.execute(f"alter table {P} rename column id to backend_product_id")
    op.execute(f"alter table {P} alter column backend_product_id drop not null")
    op.execute(f"alter table {P} add constraint products_pkey primary key (source_product_id)")
    op.execute(f"alter table {P} add constraint uq_products_backend_id unique (backend_product_id)")
    op.execute(f"alter table {P} add constraint ck_products_backend_id check (backend_product_id is null or backend_product_id > 0)")
    op.execute(f"alter table {P} add constraint fk_products_category foreign key (source_category_id) references {C}(source_category_id)")
    op.execute(f"create index ix_products_category on {P} (source_category_id)")
    op.execute(f"create index ix_products_view_count on {P} (view_count desc, source_product_id)")
