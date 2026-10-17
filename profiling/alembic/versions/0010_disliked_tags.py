"""0010 — 비선호 태그를 프로필에 (recipient_profiles.disliked_tags)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16

왜: 취향 문장에 "소니 상품이랑 사운드 관련 상품 싫어"라고 써도 좋아하는 것만 뽑아서(tag3) 이 말이 어디에도 남지 않았다.
규격 tag3disliked 는 태그마다 선호 / 비선호를 붙이고, 비선호 태그에 걸리는 상품은 30개를 고르기 전에 뺀다(DR-043).
그 비선호 태그를 챗봇이 읽을 수 있게 프로필 행에 둔다. 표는 늘리지 않는다 — 열 하나.

  recipient_profiles.disliked_tags   jsonb NOT NULL 기본 []. 모양은 preferred_tags 와 같다 [{axis, tag, source, ref}].
                                     비어 있는 이유는 tag_status 로 본다(NULL 을 쓰지 않는다). 규격이 tag3disliked 가 아니면 [].
  ix_recipient_profiles_disliked_tags  GIN — preferred_tags 와 같게(태그로 사람을 찾을 일에 대비, 지금 쓰는 질의는 없음)

이름: 0001~0005 의 disliked_tags 는 BE 비선호 카테고리 이름만 옮긴 사본이었고 0006 에서 지웠다(DR-040). 이 열은 뜻이 다르다
(글에서 뽑은 비선호 태그). 되돌릴 때는 이 마이그레이션이 먼저 지우고 0006 이 옛 열을 다시 만들므로 이름이 부딪치지 않는다.
권한: 0007 의 앱 전용 계정은 표 단위로 읽기 · 쓰기라 새 열에 따로 줄 것이 없다.
되돌리기(downgrade): 인덱스와 열을 지운다(다음 분석 때 다시 채워지는 값).
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLE = "ai_profile.recipient_profiles"
_COMMENT = ("비선호 태그 [{axis, tag, source(preference|review_text), ref(pref|r1…)}] — 글에서 싫다고 분명히 말한 것(tag3disliked). "
            "preferred_tags 와 같은 모양. 이 태그에 걸리는 상품은 30개에서 뺐다. 규격이 tag3disliked 가 아니면 [] (DR-043)")


def upgrade() -> None:
    op.execute(f"alter table {TABLE} add column disliked_tags jsonb not null default '[]'::jsonb")
    op.execute("comment on column {}.disliked_tags is '{}'".format(TABLE, _COMMENT.replace("'", "''")))   # COMMENT 는 바인드 변수를 못 받는다
    op.execute(f"create index ix_recipient_profiles_disliked_tags on {TABLE} using gin (disliked_tags)")


def downgrade() -> None:
    op.execute("drop index if exists ai_profile.ix_recipient_profiles_disliked_tags")
    op.execute(f"alter table {TABLE} drop column disliked_tags")
