"""0012 — MQ 전달 기록(ai_profile.mq_deliveries)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08

왜: BE ↔ AI 프로파일링이 HTTP 에서 RabbitMQ 로 바뀐다. BE 「프로파일링 MQ — AI 소비·결과 발행 상세 설계」(2026-11-07) §4 의
"신규 profiling_mq_deliveries 테이블 1개" 를 AI DB 에 만든다. 기존 profile_runs(분석 결과)는 그대로 두고, 메시지 한 통의
접수 · 입력 대응 · 시도 예산 · 결과 바이트 · 전달 상태를 여기에 둔다. 다시 배달되거나 중간에 죽어도 이 행을 보고 이어 간다.

설계와 다른 점 1개(11-08 사용자 결정): 원본 바이트(raw_body bytea) 대신 **변환본**(request_converted jsonb)을 둔다 —
같은 봉투 · 칸에서 글(취향 문장 · 리뷰 글)만 가드레일 정리본으로 바꾼 것(DR-039 와 같은 방식). 원본 해시는 원본 기준.

  delivery_id            UUID PK, 최초 접수 때 생성
  request_body_sha256    원본 바이트 sha256, UNIQUE — 같은 원본의 재배달을 한 행으로 묶는다
  request_converted      변환본. 손상 메시지는 NULL
  request_id · recipient_user_id · source_version
                         파싱 가능한 값. 손상 메시지는 NULL. request_id 에 UNIQUE 를 걸지 않는다(충돌 본문도 따로 보관)
  input_hash · hash_version  유효 입력에만. 분석 입력 동일성(pipeline.input_hash)
  is_binding             최초 정상 바인딩 행. 잠금 안에서 만들고 바꾸지 않는다 — request_id 하나 · (수신자, 버전) 하나에 한 행만(부분 UNIQUE)
  state                  RECEIVED / RUNNING / RETRY_WAIT / RESULT_READY / SETTLED
  analysis_attempts · next_attempt_at  커밋된 분석 시작 횟수 · 다음 실행 가능 시각. 재배달로 초기화하지 않는다
  result_body            최종 결과 메시지 바이트. 정해진 뒤 바꾸지 않는다(트리거로 막음). 손상 입력은 NULL 가능
  result_confirmed · dlq_required · dlq_confirmed  독립 전달 상태
  failure_code · created_at · updated_at

권한: 0007 의 ALTER DEFAULT PRIVILEGES 로 앱 계정이 새 표를 읽고 쓴다(따로 줄 것 없음).
되돌리기(downgrade): 표를 지운다(전달 기록만 사라지고 분석 결과 profile_runs 는 남는다).
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

T = "ai_profile.mq_deliveries"


def upgrade() -> None:
    op.execute(f"""
        create table {T} (
            delivery_id          uuid        primary key default gen_random_uuid(),
            request_body_sha256  char(64)    not null unique,
            request_converted    jsonb,
            request_id           uuid,
            recipient_user_id    bigint,
            source_version       bigint,
            input_hash           char(64),
            hash_version         text,
            is_binding           boolean     not null default false,
            state                text        not null default 'RECEIVED'
                                 check (state in ('RECEIVED', 'RUNNING', 'RETRY_WAIT', 'RESULT_READY', 'SETTLED')),
            analysis_attempts    integer     not null default 0 check (analysis_attempts >= 0),
            next_attempt_at      timestamptz,
            result_body          bytea,
            result_confirmed     boolean     not null default false,
            dlq_required         boolean     not null default false,
            dlq_confirmed        boolean     not null default false,
            failure_code         text,
            created_at           timestamptz not null default now(),
            updated_at           timestamptz not null default now(),
            constraint ck_mq_deliveries_binding_valid check (
                not is_binding or (request_id is not null and recipient_user_id is not null
                                   and source_version is not null and input_hash is not null)),
            constraint ck_mq_deliveries_result_confirmed check (not result_confirmed or result_body is not null),
            constraint ck_mq_deliveries_dlq_confirmed check (not dlq_confirmed or dlq_required),
            constraint ck_mq_deliveries_settled check (
                state <> 'SETTLED' or ((result_body is null or result_confirmed) and (not dlq_required or dlq_confirmed)))
        )""")
    op.execute(f"create unique index ux_mq_deliveries_binding_request on {T} (request_id) where is_binding")
    op.execute(f"create unique index ux_mq_deliveries_binding_key on {T} (recipient_user_id, source_version) where is_binding")
    op.execute(f"create index ix_mq_deliveries_request on {T} (request_id)")
    # HTTP 경로가 MQ 가 맡은 (수신자, 버전) 을 건드리지 않게 거르는 조회용(체크리스트 7 — stores.MQ_OWNED)
    op.execute(f"create index ix_mq_deliveries_key on {T} (recipient_user_id, source_version)")
    op.execute(f"create index ix_mq_deliveries_open on {T} (state, updated_at) where state <> 'SETTLED'")
    # 결과 바이트는 한 번 정해지면 바꾸지 않는다 — 재발행이 늘 같은 바이트가 되게(설계 §3 · §4)
    op.execute("""
        create function ai_profile.mq_deliveries_result_immutable() returns trigger language plpgsql as $$
        begin
            if old.result_body is not null and new.result_body is distinct from old.result_body then
                raise exception 'mq_deliveries.result_body 는 바꿀 수 없다';
            end if;
            return new;
        end $$""")
    op.execute(f"""create trigger tg_mq_deliveries_result_immutable before update on {T}
                   for each row execute function ai_profile.mq_deliveries_result_immutable()""")
    op.execute(f"comment on table {T} is 'MQ 요청 한 통의 접수 · 입력 대응 · 시도 예산 · 결과 바이트 · 전달 상태(0012). 원본 대신 변환본 저장'")


def downgrade() -> None:
    op.execute(f"drop table if exists {T}")
    op.execute("drop function if exists ai_profile.mq_deliveries_result_immutable()")
