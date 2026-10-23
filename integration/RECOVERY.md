# 업그레이드와 복구

마이그레이션 검증 범위는 새 DB와 `0010→0011`입니다. `0007/0008`이나 사용자 수정 DB는 별도 복사본으로 먼저 확인하세요. 이미 `0008`을 지난 DB에 수정된 `0007`을 재적용하거나 downgrade하지 않습니다.

## 백업과 마이그레이션

1. 코드·이미지 버전, 모델 lock, 활성 catalog/snapshot을 기록합니다.
2. Chat·Profiler 신규 접수를 닫고 수락된 작업을 마칩니다. Chat의 PROCESSING과 runtime lease/worker, Profiler의 큐·실행 및 DB `RUNNING/RESULT_READY/FAILED`를 함께 확인합니다. 미완료 행을 지우거나 강제로 완료 처리하지 않습니다.
3. 전체 DB를 consistent custom-format dump로 백업하고, 비밀번호 없는 역할 정의·자산 manifest·비용 예약·삭제 기록·export receipt도 보존합니다.
4. 파일 SHA256과 `pg_restore --list`를 확인한 뒤 별도 DB에 복원해 개인 프로필·미완료 작업·권한·예산 예약·삭제 기록이 유지되는지 검사합니다.

아래는 기존 libpq service/pgpass를 사용하는 예입니다. `ai_backup_source`, `ai_restore_copy`와 보호된 저장 경로는 실제 준비한 값으로 바꿉니다. DSN·비밀번호를 명령행이나 로그에 쓰지 않습니다.

```sh
pg_dump --dbname='service=ai_backup_source' --format=custom --no-password --file=/protected/backup/ai-before.dump
pg_restore --list /protected/backup/ai-before.dump > /protected/backup/ai-before.list
pg_restore --dbname='service=ai_restore_copy' --no-password --exit-on-error /protected/backup/ai-before.dump
```

원본에 `--clean`으로 복원하지 않습니다. 실제 복구는 별도 클러스터에 **같은 논리 DB 이름**을 준비합니다. 다른 이름의 검증 복사본은 내용과 권한을 검사할 수 있지만 앱의 DB identity 검사에서 차단됩니다. 이름 변경 마이그레이션은 없으므로 identity 값을 수동으로 고쳐 우회하지 않습니다. 운영 복구에는 복구 시점과 데이터 손실 범위를 별도로 결정해야 합니다.

[실행 안내의 환경 설정](README.md#실제-서비스-준비)을 마친 새 DB 또는 검증용 복사 DB에서 전용 `ai_migrator`로 실행합니다. `PROFILING_APP_DB_PASSWORD`는 설정하지 않습니다.

```sh
docker compose -f integration/containers/compose.yaml --profile migration run --rm migrate
```

이 명령은 에멧 `0011`과 통합 스키마·권한·DB identity를 적용합니다. 같은 DB·버전의 재실행은 변경 없이 끝납니다. 이미지 밖에서 실행할 때도 `MIGRATION_DATABASE_URL_FILE`과 `AI_DATABASE_NAME`을 준비하고 `python -m integration.migrate_single_server --database "$AI_DATABASE_NAME" --emmet`를 사용합니다. 기존 역할의 자격 증명은 바꾸지 않습니다.

## 장애 대응

| 상황 | 대응 |
|---|---|
| BE fetch·임베딩·새 세대 검증 실패 | 기존 활성 세대를 유지합니다. 완전 export에서 확인한 삭제 기록은 되돌리지 않습니다. |
| Search 캐시 손실 | 같은 코드·모델로 재시작하면 PG에서 복구합니다. Search 준비 후 Profiler도 명시적으로 재시작해 binding을 다시 확보합니다. 자동 재바인딩은 없습니다. |
| PG 연결 불가 | 503 상태를 유지하고 연결을 복구합니다. 오래된 메모리 상품을 제공하도록 우회하지 않습니다. |
| 삭제 상품이 저장 콜백에 포함됨 | 기존 결과를 보존하고 전송을 차단합니다. 과거 콜백의 ID를 고치거나 이미 BE가 받은 결과를 회수하지 않습니다. |
| DB 복원 필요 | 별도 DB에서 복원·검증한 뒤 연결을 바꿉니다. 삭제 상품이나 미확정 비용 예약을 복원 과정에서 되살리거나 환급하지 않습니다. |

인코더를 바꿀 때는 두 소비자의 신규 접수·claim을 닫고 재개 가능한 구세대 작업까지 비운 뒤 Search 전환 → Profiler 재바인딩 → 검증 → 접수 재개 순서를 따릅니다. 이 과정을 자동으로 조정하는 기능은 없습니다. `Store.pause_admissions(True)`는 Chat의 새 접수만 닫으므로 기존 턴 완료와 worker/lease 종료를 별도로 확인해야 합니다.

## 합성 Compose 복구

`qa-init`은 빈 시험 DB만 초기화합니다. `Never overwrite/reinitialize existing smoke data`는 기존 데이터 보호를 위한 중단입니다. 초기화 보호를 해제하거나 볼륨을 지워 우회하지 않습니다.

아래 명령은 **postgres·mesh·모의 BE·예산 fixture가 실행 중이고 Search·Profiler만 멈춘 경우**에 사용합니다. `--no-deps`로 `qa-init` 재실행을 막고, Search가 준비된 뒤 Profiler를 시작합니다.

```sh
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml up --no-deps --wait --wait-timeout 180 search
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml up --no-deps --wait --wait-timeout 180 profiler
```

최초 기동은 [합성 실행](README.md#합성-데이터로-실행)을 따릅니다. `down`으로 컨테이너를 없앤 뒤의 전체 복원은 위 두 명령의 검증 범위가 아닙니다. 합성 예산 fixture는 메모리 원장이므로 시험 도중 재시작하면 호출·예약 횟수 기록이 사라집니다.
