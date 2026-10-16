"""0011 — 상품 축 태그(products.tags) · 열린 실행 부분 색인 · prompt_version 넓히기

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16

왜: 비선호 태그(0010)로 상품을 뺄 때 매번 글자로 대조하면 다른 말('캠핑 장비' ↔ '기타캠핑용품', 'SONY' ↔ '소니')은 새고 넓은 글자
('음향기기' → TV, '레고' → '레고트')는 정상 상품까지 뺐다. 상품마다 축 태그(대표 이름)를 미리 붙여 두고 사람 태그도 같은 대표 이름으로
맞춰 겹치는지 본다(profiling.vocab). v2 는 사람이 검토한 어휘로 규칙 계산, v3 는 같은 열에 LLM 태그(폐기된 아이디어 등록부 · 보류 v3).

  products.tags                      jsonb NOT NULL 기본 [] · 배열만. [{axis, tag, via}] — axis 브랜드 · IP · 카테고리, tag 대표 이름,
                                     via 어디서(field 브랜드 칸 · 분류 / name 상품명 / v3 llm). 상품 종류는 product_kind 열에 있어 넣지 않는다.
                                     색인 없음 — 앱은 카탈로그를 메모리로 읽어 거른다(SQL 로 태그를 찾는 곳이 없다).
  catalog_versions.tags_rule         태그를 만든 규칙의 판(v2 'vocab-v1'). ┐ 셋이 함께 차거나 함께 빈다.
  catalog_versions.tags_sha256       규칙 판 + 어휘 파일의 지문.          ├ 판 행에 붙어 있어 새 카탈로그 판은 비어서 시작 → 앱은 그동안
  catalog_versions.tags_loaded_at    넣은 시각.                           ┘ 같은 함수로 그 자리에서 계산한다(결과가 같다).
                                     태그는 적재 Task(`load_catalog --tags --if-changed`)만 쓴다 — 앱 계정은 카탈로그 읽기만(0007).
  ix_profile_runs_open               열린 실행(RUNNING · RESULT_READY)만 담는 부분 색인 — /health 의 미전달 수 · 시작 때 정리가
                                     표 전체를 훑지 않게(BE 가 /health 를 핑으로 자주 부른다).
  prompt_version → text              프로필 · 실행 기록 둘 다. varchar(80) 은 '<규격>-<해시>@<모델>' 에서 모델 이름 몫이 58자뿐이라
                                     긴 모델 이름이면 저장이 실패했다(DB 점검 2026-10-16).

권한: 0007 의 앱 계정은 표 단위 권한이라 새 열에 따로 줄 것이 없다(카탈로그는 읽기만).
되돌리기(downgrade): 색인 · 제약 · 열을 지운다. prompt_version 은 text 로 둔다 — 80자를 넘는 값이 있으면 줄이기가 실패하고,
넓은 채로 두어도 무해하다(0006 을 되돌리면 열째로 지워진다).
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

PRODUCTS = "ai_catalog.products"
VERSIONS = "ai_catalog.catalog_versions"
_TAGS_COMMENT = ("상품 축 태그 [{axis(브랜드|IP|카테고리), tag(대표 이름), via(field|name|llm)}] — 비선호 태그와 같은 대표 이름으로 맞대 본다. "
                 "판은 catalog_versions.tags_rule · tags_sha256 (0011)")


def _comment(target: str, text: str) -> None:
    op.execute("comment on column {} is '{}'".format(target, text.replace("'", "''")))   # COMMENT 는 바인드 변수를 못 받는다


def upgrade() -> None:
    op.execute(f"alter table {PRODUCTS} add column tags jsonb not null default '[]'::jsonb")
    op.execute(f"alter table {PRODUCTS} add constraint ck_products_tags_array check (jsonb_typeof(tags) = 'array')")
    _comment(f"{PRODUCTS}.tags", _TAGS_COMMENT)

    op.execute(f"alter table {VERSIONS} add column tags_rule text, add column tags_sha256 char(64), "
               "add column tags_loaded_at timestamptz")
    op.execute(f"""alter table {VERSIONS} add constraint ck_catalog_versions_tags_all_or_none check (
                       (tags_rule is null) = (tags_sha256 is null) and (tags_rule is null) = (tags_loaded_at is null))""")
    _comment(f"{VERSIONS}.tags_rule", "상품 태그를 만든 규칙의 판(v2 vocab-v1 · v3 LLM 판). NULL = 이 판에는 상품 태그가 없다")
    _comment(f"{VERSIONS}.tags_sha256", "규칙 판 + 어휘 파일 지문 — 앱 · 적재 도구의 지문과 다르면 다시 계산해 교체")
    _comment(f"{VERSIONS}.tags_loaded_at", "상품 태그를 넣은 시각")

    op.execute("create index ix_profile_runs_open on ai_profile.profile_runs (status, updated_at) "
               "where status in ('RUNNING', 'RESULT_READY')")
    op.execute("alter table ai_profile.profile_runs alter column prompt_version type text")
    op.execute("alter table ai_profile.recipient_profiles alter column prompt_version type text")


def downgrade() -> None:
    op.execute("drop index if exists ai_profile.ix_profile_runs_open")
    op.execute(f"alter table {VERSIONS} drop constraint if exists ck_catalog_versions_tags_all_or_none")
    op.execute(f"alter table {VERSIONS} drop column tags_loaded_at, drop column tags_sha256, drop column tags_rule")
    op.execute(f"alter table {PRODUCTS} drop constraint if exists ck_products_tags_array")
    op.execute(f"alter table {PRODUCTS} drop column tags")
