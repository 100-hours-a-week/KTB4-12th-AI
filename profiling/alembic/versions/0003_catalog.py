"""0003 — ai_catalog (상품 카탈로그: 카테고리 2단 · 상품 · 적재 버전)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

입력은 Backend 전달 패키지 `product-catalog-20260922-v1` (상품 4,231 · 대분류 10 + 소분류 57, 분류 버전 2026-09-15.final57).
그 패키지의 `schemas/product.schema.json`·`category.schema.json`을 그대로 옮긴 표다 — 필드를 만들거나 합치지 않는다.

**ID 규칙이 이 표의 핵심이다.**
  source_*_id   `KAKAO_GIFT:10002797` · `CAT-01-02` · `GROUP-01` — 수집처 자연키. 지금 우리가 가진 유일한 키이므로 PK.
  backend_*_id  Backend가 발급할 BIGINT. 패키지 `mapping-templates/*.jsonl`이 전부 null이라 **아직 없다** → nullable.
                7.6의 dislikedCategories[].categoryId 와 7.7의 recommendedProductIds 는 이 열의 값이다.
                채우기 전에는 7.7로 내보낼 수 없다 (Backend에 없는 번호가 된다).

미확인 값은 미확인으로 둔다 (패키지 README 지시):
  stock_quantity · available  전건 NULL. "품절 0"이나 "판매가능 true"로 바꾸지 않는다.
  view_count                  패키지에 없음 → NULL. v1 풀 정렬 기준이므로 Backend 7.9 export가 와야 채워진다.
  둘 다 Backend 7.9 export(`available` = quantity > 0, `views`)로 갱신할 자리다.

catalog_versions: 적재 1회 = 1행. 활성은 항상 1개(부분 유니크 인덱스)이고 그 id(uuid)가
profile.types.SearchResult.catalog_version_id 에 들어간다 — 파일 카탈로그의 고정 UUID를 대신한다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "ai_catalog"
PRODUCT_TYPES = ("Shipping", "Pickup", "Voucher")    # 패키지 product.schema.json 의 enum 그대로


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ---- 적재 버전 ----------------------------------------------------------
    op.create_table(
        "catalog_versions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("package_id", sa.Text, nullable=False, comment="전달 패키지 이름 (product-catalog-20260922-v1)"),
        sa.Column("taxonomy_version", sa.Text, nullable=False, comment="분류 버전 (2026-09-15.final57)"),
        sa.Column("product_count", sa.Integer, nullable=False),
        sa.Column("category_count", sa.Integer, nullable=False),
        sa.Column("source_sha256", pg.JSONB, nullable=True, comment="패키지 summary.json 의 원본 파일 해시 — 같은 패키지인지 대조용"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("false"), comment="활성 버전은 항상 한 개"),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema=SCHEMA,
    )
    op.create_index("one_active_catalog_version", "catalog_versions", ["is_active"], unique=True,
                    schema=SCHEMA, postgresql_where=sa.text("is_active"))

    # ---- 카테고리 (대분류 level 1 · 소분류 level 2) ----------------------------
    op.create_table(
        "categories",
        sa.Column("source_category_id", sa.Text, primary_key=True, comment="GROUP-01(대분류) · CAT-01-02(소분류). 수집처 문자열 ID"),
        sa.Column("parent_source_category_id", sa.Text, nullable=True, comment="소분류만 값이 있다"),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("level", sa.SmallInteger, nullable=False, comment="1 대분류 · 2 소분류"),
        sa.Column("product_count", sa.Integer, nullable=False, comment="패키지가 선언한 수 (적재 후 실제 수와 대조)"),
        sa.Column("backend_category_id", sa.BigInteger, nullable=True,
                  comment="Backend categories.id. 회신 전까지 NULL — 7.6 dislikedCategories[].categoryId 가 이 값"),
        sa.Column("taxonomy_version", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_source_category_id"], [f"{SCHEMA}.categories.source_category_id"],
                                name="fk_categories_parent"),
        sa.CheckConstraint("level IN (1, 2)", name="ck_categories_level"),
        sa.CheckConstraint("(level = 1) = (parent_source_category_id IS NULL)", name="ck_categories_parent_by_level"),
        sa.UniqueConstraint("backend_category_id", name="uq_categories_backend_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_categories_parent", "categories", ["parent_source_category_id"], schema=SCHEMA)

    # ---- 상품 --------------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("source_product_id", sa.Text, primary_key=True, comment="KAKAO_GIFT:10002797. 수집처 자연키"),
        sa.Column("backend_product_id", sa.BigInteger, nullable=True,
                  comment="Backend products.id. 회신 전까지 NULL — 7.7 recommendedProductIds 가 이 값"),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("brand", sa.Text, nullable=False),
        sa.Column("source_category_id", sa.Text, nullable=False, comment="소분류만 온다 (level 2)"),
        sa.Column("product_kind", sa.Text, nullable=False, comment="립밤·이어폰 같은 종류 텍스트"),
        sa.Column("product_type", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"),
                  comment="문자열 키/값. 1,446건은 빈 객체 — 정형 사양으로 가정하지 않는다"),
        sa.Column("unit_price", sa.Integer, nullable=False),
        sa.Column("list_price", sa.Integer, nullable=True, comment="정가. 충돌·미확인 4건은 NULL — 할인율을 만들지 않는다"),
        sa.Column("currency", sa.Text, nullable=False, server_default=sa.text("'KRW'")),
        sa.Column("stock_quantity", sa.Integer, nullable=True, comment="미확인 = NULL. 패키지는 전건 NULL"),
        sa.Column("available", sa.Boolean, nullable=True, comment="미확인 = NULL. Backend 7.9의 quantity > 0 으로 채운다"),
        sa.Column("view_count", sa.Integer, nullable=True, comment="패키지에 없음 = NULL. Backend 7.9의 views 로 채운다 (v1 풀 정렬 기준)"),
        sa.Column("source_provider", sa.Text, nullable=False),
        sa.Column("source_product_url", sa.Text, nullable=False),
        sa.Column("source_image_url", sa.Text, nullable=False),
        sa.Column("image_asset_id", sa.Text, nullable=True, comment="data/image-assets.json 의 변환 이미지 3종 키"),
        sa.Column("package_id", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_category_id"], [f"{SCHEMA}.categories.source_category_id"],
                                name="fk_products_category"),
        sa.CheckConstraint("unit_price >= 0", name="ck_products_unit_price"),
        sa.CheckConstraint("list_price IS NULL OR list_price >= 0", name="ck_products_list_price"),
        sa.CheckConstraint(f"product_type IN {PRODUCT_TYPES}", name="ck_products_type"),
        sa.CheckConstraint("backend_product_id IS NULL OR backend_product_id > 0", name="ck_products_backend_id"),
        sa.UniqueConstraint("backend_product_id", name="uq_products_backend_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_products_category", "products", ["source_category_id"], schema=SCHEMA)
    op.create_index("ix_products_view_count", "products", [sa.text("view_count DESC"), "source_product_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_products_view_count", table_name="products", schema=SCHEMA)
    op.drop_index("ix_products_category", table_name="products", schema=SCHEMA)
    op.drop_table("products", schema=SCHEMA)
    op.drop_index("ix_categories_parent", table_name="categories", schema=SCHEMA)
    op.drop_table("categories", schema=SCHEMA)
    op.drop_index("one_active_catalog_version", table_name="catalog_versions", schema=SCHEMA)
    op.drop_table("catalog_versions", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
