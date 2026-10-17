# 업그레이드·백업·복구

현재 경로는 PostgreSQL 18 + pgvector, Emmet Alembic 0011, 통합 single-server/1입니다. 앱은 DDL을 실행하지 않습니다. 운영 전환은 배포 담당자의 작업이며 후보 준비 중 기존 DB·역할·배포를 변경하지 않았습니다.

## 업그레이드 전

1. 코드 commit, 이미지 digest, model lock/encoder fingerprint, 활성 catalog/snapshot, taxonomy hash를 기록합니다.
2. Chat·Profiler 신규 접수를 닫고 수락된 작업을 완료시킵니다. Chat PROCESSING·worker/runtime lease, Profiler 큐·실행 및 DB RUNNING/RESULT_READY/FAILED를 함께 확인합니다. 미완료 행을 강제 삭제·완료 처리하지 않습니다. 전체 자동 조정기는 없으므로 운영 경로 미확정이면 전환을 보류합니다.
3. 승인된 PostgreSQL 접속으로 전체 DB consistent custom-format dump를 저장소 밖 보호 위치에 저장합니다. DSN·비밀번호는 명령행/로그에 쓰지 않습니다. 역할 정의는 비밀번호 없는 기존 운영 백업 절차를 따릅니다. 자산 manifest·원장·tombstone·export receipts도 보존합니다.
4. SHA256과 pg_restore --list 성공을 기록하고 사전 준비한 별도 복구 DB에 복원·검증합니다. 원본을 덮는 --clean 복원이나 downgrade는 하지 않습니다.

기존 libpq service/pgpass가 준비된 환경의 예입니다. 이름과 경로는 담당자가 정합니다.

```sh
pg_dump --dbname='service=ai_backup_source' --format=custom --no-password --file=/protected/backup/ai-before.dump
pg_restore --list /protected/backup/ai-before.dump > /protected/backup/ai-before.list
pg_restore --dbname='service=ai_restore_copy' --no-password --exit-on-error /protected/backup/ai-before.dump
```

## 명시적 migration

같은 준비 DB의 ai_migrator만 사용하며 PROFILING_APP_DB_PASSWORD는 설정하지 않습니다. 지정 DB의 기존 앱 소유 테이블을 migration 소유자로 옮길 권한을 미리 확인합니다.

```sh
docker compose -f integration/containers/compose.guarded.yaml --profile migration run --rm migrate
```

Emmet 0011 후 통합 스키마·권한·identity를 적용하며 같은 버전 재실행은 no-op입니다. 이미 0008을 지난 DB에 수정된 0007을 재적용하지 않습니다. 검증한 upgrade 출발점은 **0010**입니다. 임의의 옛 0007/0008/사용자 수정 DB는 별도 복사 검증 없이 지원 완료로 간주하지 않습니다. 개인 프로필·callback을 보존하고 Chat/Profiler에 catalog SQL 권한을 주지 않습니다. grant를 임의 발급하거나 예산을 초기화하지 않습니다.

## 장애별 복구

| 상황 | 대응 |
|---|---|
| BE fetch·embedding·pending 검증 실패 | 기존 active 유지. 확인된 tombstone 보존, 부분 export로 삭제 추정 금지 |
| Search 캐시 손실 | 동일 모델/코드로 PG에서 같은 세대 재구성. Profiler lease는 명시적 refresh/재시작으로 재획득 |
| PG 확인 불가 | 503 유지. 오래된 메모리 상품을 제공하도록 우회하지 않음 |
| 코드/encoder 전환 실패 | 접수 중지 유지. 구세대 재개 가능 작업·DB schema 확인 후 검증된 이미지/encoder 쌍으로 재시작 |
| DB 복구 필요 | 별도 DB에 복원하고 identity·개인 프로필·미완료 작업·예산 예약·tombstone·export receipt 검증 후 담당자가 연결 전환 |

DB rollback과 과거 catalog 선택은 다릅니다. 삭제된 상품과 지출/미확정 비용을 되살리거나 환급하면 안 됩니다. 사용자 상태를 잃는 복원에는 복구 시점·데이터 손실 결정이 필요합니다. 합성 backup/restore 시험은 실제 운영 백업 정책·RPO/RTO 보증이 아닙니다.

DB identity에는 원래 논리 DB 이름이 들어 있습니다. 실제 복구는 별도 클러스터에 같은 DB 이름을 준비하는 것이 기본입니다. 다른 이름으로 만든 검증 복사본은 내용·권한을 확인할 수 있지만 앱 기동은 의도적으로 차단됩니다. 이름 변경용 identity migration은 구현하지 않았으며 수동 값을 바꾸어 우회하지 않습니다.
