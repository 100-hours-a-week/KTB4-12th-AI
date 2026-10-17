# BE 상품 export 계약 초안

현재 Search가 받는 형식입니다. **BE와 아직 합의·연결하지 않았으며**, 예시는 [합성 fixture](contracts/be-export-draft.fixture.json)입니다. 개인 취향·리뷰·비선호는 상품 export에 넣지 않습니다.

```json
{"data":{"generatedAt":"2026-10-16T02:00:00Z","products":[],"snapshot":{"id":"<UUID>","complete":true,"productCount":0,"sha256":"<products+categories canonical SHA256>"}}}
```

| 필드 | 처리 규칙 |
|---|---|
| `productId`, `categoryId` | 양의 JS-safe 정수. 문자열·bool 거부. 상품은 ID 오름차순이며 중복 불가 |
| `name`, `brand`, `categoryName` | 문자열. `categoryName`은 해당 소분류 이름과 일치 |
| `description` | 문자열 또는 null. null은 빈 설명으로 처리 |
| `price`, `available` | 비음수 정수·bool. 판매 불가는 행을 유지하고 unavailable로 표시 |
| `viewCount` | 명시적 0은 0. 누락/null은 기존 값 유지, 신규 상품은 0 |
| `updatedAt`, `generatedAt` | UTC 문자열. 상품 시각 ≤ export 시각. 이전 export보다 오래된 발행 거부 |
| `snapshot` | UUID·`complete=true`·상품 수·digest 필수. 같은 ID에 다른 내용은 거부 |
| categories | 중복 없는 대분류·소분류 관계. 상품은 존재하는 소분류를 참조 |
| 상품 종류·이미지·source ID | 기존 출처 보완 정보를 유지하며 신규 값은 추정하지 않음 |

상품과 categories는 같은 시점의 전체 자료여야 합니다. [digest 구현](../product-search/search/complete_export.py)과 count가 맞지 않으면 받지 않습니다. BE와는 전체 export 범위·정렬, 분류 변경 순서, 품절과 삭제의 차이, 조회수 누락 의미를 합의해야 합니다.

완전성이 확인된 export에서 사라진 상품만 삭제 기록(tombstone)에 넣습니다. 이후 임베딩 실패나 과거 세대 선택이 있어도 해당 상품은 검색·콜백에서 제외됩니다. ID 재사용과 삭제 복원은 별도 계약이 필요합니다. 부분 export의 누락을 삭제로 처리하지 않습니다.

동기화는 [실행 안내](README.md#실제-서비스-준비)의 `catalog-sync` 작업으로 실행합니다. 기본 CLI는 dry run이며 `--apply`가 있어야 새 세대를 준비합니다. Search가 검증·활성화할 때까지 기존 세대로 검색합니다. 가격·조회수·판매 상태만 바뀌면 기존 임베딩을 재사용합니다.

직접 CLI를 실행할 때는 `AI_DATABASE_NAME`, `CATALOG_SYNC_DATABASE_URL_FILE`, `BACKEND_SERVICE_TOKEN_FILE`, `SEARCH_RUNTIME_ROOT`, `ASSET_ROOT`, `ENCODER_CODE_ROOT`을 준비합니다. URL 두 개는 같은 origin의 HTTPS를 사용하고 인증 redirect를 따르지 않습니다. 별도 승인된 사설 HTTP는 프로세스 환경에 `BACKEND_EXPORT_ALLOW_PRIVATE_HTTP=1`이 필요하며 기본 Compose가 이 설정을 전달하지는 않습니다.

## 사전 계산 파일을 재사용할 때

실제 파일의 연결과 import는 미완료입니다. 먼저 상품·분류 ID와 행 순서, 벡터 SHA·float32·행 수×768·유한 비영 값, 모델 revision·weight SHA·encoder fingerprint·전처리·거리 계약이 일치하는지 확인합니다. 상품 태그는 RULE+TSV hash, 브랜드 집합과 태그 내용도 맞아야 합니다. 근거가 없는 manifest 필드를 현재 값으로 채우지 않습니다.

묶음 이름이나 부분 ID 일치만으로 같은 카탈로그라고 볼 수 없습니다. 기존 파일을 쓰더라도 완전 export의 삭제 기록과 발행 근거는 보존해야 합니다. 모델 파일 기준은 [model.lock.json](../product-search/embedding/model.lock.json), 실행 중인 세대 기준은 `/v1/catalog/contract`입니다.
