# BE ↔ AI 연동 필드표 v1 — 7.6 · 7.7 과 `profileStatus` (2026-09-22 작성 · 2026-09-29 갱신 — 같은 번호 재시도 · 즉시 3회 · 503 두 가지 · 비선호 5)

AI 프로파일링 서비스가 **지금 실제로 보내고 받는 필드**와 **`profileStatus`가 언제 어떻게 바뀌는지**를 상황별로 적은 표. 계약 원문은 모델 API 설계서(작업본 v3.2.7)이고, 위키(v3.2.6)와 다른 곳은 ⚠로 표시했다. **v1 범위만** 적었다: 입력은 비선호 카테고리뿐이고, 상품 목록은 BE가 넘겨주는 파일을 AI가 수동 적재한다. 다음 판: [v2](BE_연동_필드표_v2.md)(7.9 상품 export API) · [v3](BE_연동_필드표_v3.md)(취향·리뷰 입력). 색인: [BE_연동_필드표.md](BE_연동_필드표.md).

| 절 | 내용 |
|---|---|
| 1 | 7.6 — 요청·202·오류 필드 |
| 2 | 7.7 — 요청·200·오류 필드와 AI 처리 |
| 3 | **`profileStatus` 생애주기** — 값·저장 열·전이표·사건별 규칙·재시도·경합 |
| 4 | **시퀀스 다이어그램** — 성공 1 · 실패 4 |
| 5 | BE에 확인·요청 |
| 6 | 부록 — AI 오류 코드 |

## 0. 공통

| 항목 | 값 |
|---|---|
| 인증 | 두 API 모두 `Authorization: Bearer <serviceToken>`. AI는 **토큰 하나**를 7.6 검사와 7.7 송신에 같이 쓴다 (`PROFILING_SERVICE_TOKEN`). BE와 같은 값이어야 함 |
| 성공 봉투 | `{"message": "...", "data": {...}}` |
| 오류 봉투 | `{"message": "...", "error": {"code": "...", "traceId": null}}` — `traceId`는 현재 `null`(문자열이 필요하면 uuid를 넣을 수 있음) |
| HTTP 코드 | 계약대로. **필드가 안 맞을 때 원인 구분은 AI 내부 코드**(§6)로 남기고 HTTP 코드는 바꾸지 않는다 |
| 모르는 필드 | AI가 **받는** 본문(7.6)에 계약에 없는 필드가 있어도 거부하지 않는다(무시 + 경고 로그). BE가 필드를 추가해도 연동이 깨지지 않음 |
| 상품 목록 (v1) | BE가 넘겨준 상품 목록 파일(`productId` = BE `products.id`, `categoryId` = `categories.id`, `views` 포함)을 AI가 적재해 "활성 카탈로그"로 쓴다. 이게 없으면 7.6은 503 |

---

## 1. 7.6 수신자 프로파일링 웹훅 — BE → AI

`POST {AI}/api/internal/v1/ai/profile/extract-and-pool` · `Content-Type: application/json`

### 요청 필드 (BE가 보냄)

| 필드 | 타입 | 필수 | null | 제약 | v1 값 | 비고 |
|---|---|---|---|---|---|---|
| `recipientUserId` | integer | ✔ | ✕ | > 0 | 수신자 users.id | |
| `sourceVersion` | integer | ✔ | ✕ | ≥ 0 | **BE가 7.6을 보내기 직전에 +1 하고 저장한 번호** (§3.5) | 7.7에 그대로 돌아옴 → BE는 이 값이 마지막으로 보낸 번호와 같은지로 **최신 여부**를 안다 |
| `dislikedCategories` | array | ✔ | ✕ | 없으면 `[]` (키 생략 불가) | 비선호 카테고리 목록 | **최대 5** — BE 화면 규칙(`PreferencePolicy.MAX_SELECTABLE_COUNT`)과 같은 값. AI 도 6개 이상이면 400 `INVALID_REQUEST`(09-29) |
| `dislikedCategories[].categoryId` | integer | ✔ | ✕ | > 0 | categories.id — **대분류(root) id 1~10** | BE는 root 카테고리만 비선호로 저장한다(`PreferenceSaveService`, 최대 5 · 09-27 결정: 사용자 친화성). AI는 그 대분류의 **하위 소분류 전체**를 풀에서 제외한다 |
| `dislikedCategories[].categoryName` | string | ✔ | ✕ | | categories.name | ID가 안 맞아도 이름으로 한 번 더 거른다 |

v1 본문은 이 **세 필드가 전부**다. (⚠ 계약 원문에는 `giftPreference`·`reviews` 키가 더 있으나 v3 입력이라 v1에서는 보내지 않는다. AI는 없으면 `null`·`[]`로 처리하고, 보내도 받는다.)

```json
{"recipientUserId": 9871, "sourceVersion": 3,
 "dislikedCategories": [{"categoryId": 1, "categoryName": "뷰티"}]}
```

### 응답 — 정상 `202 Accepted`

| 필드 | 값 |
|---|---|
| `message` | `"프로파일 분석이 시작되었습니다."` |
| `data.recipientUserId` | 요청 값 그대로 |
| `data.sourceVersion` | 요청 값 그대로 (AI는 바꾸지 않음) |
| `data.profileStatus` | 항상 `"PENDING"` |

```json
{"message": "프로파일 분석이 시작되었습니다.", "data": {"recipientUserId": 9871, "sourceVersion": 3, "profileStatus": "PENDING"}}
```

202 뒤 처리는 백그라운드. 결과는 **7.7 콜백으로만** 온다. 실패해도 AI는 7.6·7.7로 `FAILED`를 보내지 않는다 — BE가 `PENDING` 지속 시간으로 판정. **202를 받았을 때 BE가 `profileStatus`를 어떻게 바꿔야 하는지는 §3.4 ②(조건부 PENDING)**.

### 응답 — 오류 (AI가 돌려주는 것)

| HTTP | `error.code` | 언제 | `message` 예시 (실제 값) | BE 처리 |
|---|---|---|---|---|
| 400 | `INVALID_REQUEST` | 본문이 JSON이 아님 | `요청 본문이 JSON이 아닙니다.` | 재시도 없음 |
| 400 | `INVALID_REQUEST` | 필수 키 누락 | `요청 형식이 올바르지 않습니다: dislikedCategories — Field required` | 재시도 없음. **어느 필드인지 message에 경로** |
| 400 | `INVALID_REQUEST` | 타입 오류 | `요청 형식이 올바르지 않습니다: recipientUserId — Input should be a valid integer, unable to parse string as an integer` | 〃 |
| 400 | `INVALID_REQUEST` | 범위 | `… recipientUserId — Input should be greater than 0` | 〃 |
| 400 | `INVALID_REQUEST` | 하위 필드 누락 | `… dislikedCategories.0.categoryName — Field required` | 〃 |
| 400 | `INVALID_REQUEST` | 목록 상한 초과 (비선호 5 · 리뷰 10) | `… dislikedCategories — List should have at most 5 items after validation, not 6` | 〃 |
| 401 | `UNAUTHORIZED` | `Authorization` 없음 / `Bearer ` 아님 | `서비스 토큰이 없습니다.` | 토큰 설정 확인 |
| 401 | `UNAUTHORIZED` | 토큰 값 다름 | `서비스 토큰이 올바르지 않습니다.` | 〃 |
| 503 | `SERVICE_UNAVAILABLE` | AI에 활성 카탈로그(상품 목록 적재본)가 없음 · **또는 접수 대기열 가득(09-28)** | `활성 카탈로그가 없습니다.` · `접수 대기열이 가득 찼습니다.` | `Retry-After`(카탈로그 없음 300초 · 대기열 가득 30초) 뒤 **같은 번호** 재시도 (§3.6). 상태 불변. **헤더 `Retry-After: 300` 을 실제로 보낸다**(09-26 구현) |
| 500 | `INTERNAL_SERVER_ERROR` | AI 내부 오류 | `서버 오류가 발생했습니다.` | 백오프 뒤 **같은 번호** 재시도 (§3.6). 상태 불변 |

오류가 **아닌** 것: 계약에 없는 필드(예: `dislikedCategories[].weight`, 최상위 `extra`) → **202** 정상 접수, AI 로그에 `CONTRACT_7_6_UNKNOWN_FIELD unknown={...}` 경고만.

---

## 2. 7.7 추천 결과 콜백 — AI → BE

`POST {BE}/api/internal/v1/recipients/{recipientUserId}/profile` · `Content-Type: application/json` · `Authorization: Bearer <serviceToken>`

7.6 처리 후 **성공한 경우에만** 보낸다. 같은 (수신자, sourceVersion)에 대해 같은 본문을 다시 보낼 수 있다(재전송) — BE는 같은 결과를 다시 받아도 200이어야 한다. 비선호·재고로 전부 걸러져 **0개여도 성공**이다 — `recommendedProductIds: []`로 보내며, BE는 "저장된 추천 없음 → 인기순"으로 처리해 달라(09-27, [BE_전달 §7 8번](BE_전달_2026-09-27.md)).

### 요청 필드 (AI가 보냄)

| 필드 | 타입 | 값 | 비고 |
|---|---|---|---|
| Path `recipientUserId` | integer | 7.6의 값 | |
| `recipientUserId` | integer | Path와 같은 값 | 다르면 BE가 400 `RECIPIENT_ID_MISMATCH` |
| `sourceVersion` | integer | 7.6에서 받은 값 그대로 | BE의 순서 역전 판정 키 |
| `profileStatus` | string | 항상 `"COMPLETED"` | |
| `recommendedProductIds` | integer[] | **최대 30개**, 배열 순서 = 추천 순위 | 값은 **BE `products.id`** (AI 상품 목록의 `productId`). 비선호 대분류의 하위 소분류 전체를 제외한 뒤 조회수(`views`)순 |
| ~~`preferredTags`~~ · ~~`dislikedTags`~~ | — | **보내지 않음** | ⚠ 위키(v3.2.6)는 필수. 작업본 v3.2.7(DR-035: 태그는 AI 보관)은 없음. **BE가 위키대로 필수 검증하면 400** → 없어도 받도록 하거나, 정하면 AI가 `[]`로 동봉 |

```json
{"recipientUserId": 9871, "sourceVersion": 3, "profileStatus": "COMPLETED",
 "recommendedProductIds": [101, 205, 318, 77, 4312, 990, 12, 55, 2048, 731, 16, 908, 33, 1200, 87, 41, 610, 2, 375, 88, 913, 60, 4421, 19, 502, 7, 1301, 66, 840, 250]}
```

### 응답 — AI가 기대하는 정상 `200 OK`

| 필드 | 값 |
|---|---|
| `message` | 자유 (예: `"수신자 프로필이 성공적으로 저장되었습니다."`) |
| `data.recipientUserId` · `data.sourceVersion` | 요청 값 그대로 |
| `data.profileStatus` | `"COMPLETED"` |

AI는 **HTTP 상태 코드로만** 분기한다. 본문은 로그용이라 형식이 달라도 동작에는 영향 없다.

### 응답 — 오류 (BE가 돌려주는 것)와 AI의 처리

| HTTP | `error.code` (제안) | 언제 | AI 처리 | AI 실행 기록 (`profile_runs`) |
|---|---|---|---|---|
| 400 | `RECIPIENT_ID_MISMATCH` | Path ≠ 본문 `recipientUserId` | 재시도 없음 | `status=FAILED`, `error.code=CONTRACT_7_7_REJECTED`, `reason="400 RECIPIENT_ID_MISMATCH"` |
| 400 | `INVALID_REQUEST` | 필수 필드 누락·타입·ID 31개 이상 | 재시도 없음 | 〃 (`reason="400 INVALID_REQUEST"`) |
| 401 / 403 | `UNAUTHORIZED` / `FORBIDDEN` | 토큰 | 재시도 없음 | 〃 (`reason="401 UNAUTHORIZED"`) |
| 409 | `STALE_SOURCE_VERSION` | 순서 역전 — BE에 더 새 `sourceVersion` 결과가 이미 있음 | **폐기**, 재시도 없음 | `status=SUPERSEDED`, `error.code=CALLBACK_STALE` |
| 500 / 503 | `INTERNAL_SERVER_ERROR` / `SERVICE_UNAVAILABLE` | BE 오류 | 같은 슬롯에서 **즉시 3회**(0.5초·2초 뒤) 다시 보내고, 그래도 실패면 결과를 보관해 **재전송 대상**으로 남김(§6.2) | `status=RESULT_READY`, `error.code=CALLBACK_UNREACHABLE`, `reason="503 SERVICE_UNAVAILABLE"` |
| 연결 실패·타임아웃(5초) | — | BE 안 뜸, 네트워크 | 〃 | `status=RESULT_READY`, `error.code=CALLBACK_UNREACHABLE`, `reason="ConnectTimeout: …"` |

BE에 부탁: **400과 409는 위 조건대로 구분**해 달라. 409를 400으로 주면 AI는 "계약 불일치"로 기록해 원인을 찾기 어렵다.

---

## 3. `profileStatus` 생애주기 — BE가 저장·판정, AI는 값을 보내기만

> **재시도는 같은 번호다 (09-27, BE 계획 5-1·5-2 로 통일).** 새 요청만 새 번호이고, 그 번호가 실패하면 — 접수 실패(500·503·연결 실패·타임아웃)든 결과 미도달(PENDING 타임아웃)이든 — **같은 번호로 최대 2회** 다시 보낸다. `retry_count` 하나가 둘 다 센다.
>
> | | 언제 | AI가 가진 것 | 번호 | 횟수 |
> |---|---|---|---|---|
> | **재시도** (BE가 하는 일) | 7.6이 500·503·연결 실패·타임아웃으로 접수 안 됨 · 또는 202 뒤 7.7이 10분 안에 안 옴 | 접수 안 됐으면 없음, 접수됐으면 결과가 있을 수 있다 | **같은 번호** | `retry_count` 최대 2, 다 쓰면 FAILED |
> | **재전송** (AI가 하는 일) | 같은 번호가 다시 왔는데 결과가 이미 있을 때 | 저장된 콜백 본문 | 같은 번호 | 재분석 없음 |
>
> AI는 같은 번호를 실행 기록으로 가른다: 기록이 없으면 분석하고, 결과가 있으면 재분석 없이 재전송하며, 본문이 다르면 다시 분석한다(`intake.decide()`). 400·401은 재시도하지 않는다(요청을 고친 뒤 새 번호).

### 3.1 값 네 개

| 값 | 뜻 | 누가 · 언제 세팅 | 화면(상품 목록)에서 |
|---|---|---|---|
| `NONE` | 분석할 자료가 없는 콜드스타트 (이때 `sourceVersion = 0`) | **BE** — 수신자 생성 시 기본값 | 개인화 없음 → `POPULAR` 등 대체 정렬 |
| `PENDING` | 7.6이 접수됐고 결과(7.7)를 기다리는 중 | **AI**가 7.6 응답(202)으로 보내고 **BE가 조건부로 저장**(§3.4 ②) | 이전 결과가 있으면 그것을 계속 보여줌 |
| `COMPLETED` | 7.7 결과(30개)가 저장됨 | **AI**가 7.7로 보내고 BE가 버전 판정 후 저장 | 30개를 `AI_RECOMMENDED`로 우선 노출 |
| `FAILED` | PENDING이 너무 오래 지속 | **BE만** 판정(§3.4). AI는 FAILED를 보내지 않는다 | 이전 결과가 있으면 그것, 없으면 대체 정렬 |

### 3.2 BE가 수신자마다 가지고 있어야 하는 값 (현재 Table-Specification에 없음 — 제안)

| 열 (제안) | 뜻 | 바뀌는 때 |
|---|---|---|
| `profile_status` | 위 네 값 | §3.3 전이표 |
| `source_version` | **마지막으로 보낸 7.6의 번호** | 7.6을 **보내기 직전에 +1 하고 저장**(요청보다 먼저 커밋). ⚠ 계약 원문은 "수정할 때마다 +1"이지만 보낼 때 +1로 결정 — §3.5 |
| `analyzed_source_version` | **저장된 7.7 결과의 번호** | 7.7 저장 때 = 본문의 `sourceVersion`. **`analyzed == source_version` 이면 결과가 최신** |
| `recommended_product_ids` | 7.7의 30개 | 7.7 저장 때 |
| `last_changed_at` | 마지막 변경 시각 (디바운스 기준, 비어 있으면 "대기 중인 수정 없음") | 변경 때 기록 · 7.6 202 때 **조건부로** 비움 |
| `window_started_at` | 최초 미반영 변경 시각 (6시간 상한 기준) | 첫 변경 때 기록 · 7.6 202 때 비움 |
| `pending_since` | PENDING이 된 시각 (타임아웃 판정) | 7.6 202 때 |
| `retry_count` | 같은 번호로 다시 보낸 횟수 (최대 2) — 접수 실패와 결과 미도달을 **합산**한다. BE 제안 `TINYINT UNSIGNED NOT NULL DEFAULT 0` (09-25) · 09-27 BE 계획 5-1은 `last_attempt_at`·`next_retry_at`도 둔다 | 재시도 직전 +1 · **새 번호 요청이나 7.7 저장 성공 때 0** |

### 3.3 전이표

| 지금 상태 → 사건 | 7.6 → **202** | 7.6 → **400 / 401** | 7.6 → **503 / 500 / 응답 없음** | 7.7 **200** 저장 | 7.7 **409** (순서 역전) | PENDING **타임아웃** | 사용자가 비선호 수정 |
|---|---|---|---|---|---|---|---|
| `NONE` | → **PENDING** | 그대로 + 알림 | 그대로, `last_changed_at` 유지 → 재시도 | → **COMPLETED** | (없음) | — | `last_changed_at` 기록 (번호는 안 올림) |
| `PENDING` | 그대로 PENDING (새 번호로 재요청) | 그대로 + 알림 | 그대로 → 재시도 | 30개 저장. 번호 = 마지막 보낸 번호면 → **COMPLETED**, 더 낮으면(새 요청 진행 중) **PENDING 유지** | 거부, 그대로 | → **FAILED** | 기록. **상태는 PENDING 유지** |
| `COMPLETED` | → **PENDING** (옛 30개는 계속 보여줌) | 그대로 + 알림 | 그대로(옛 30개 유지) → 재시도 | → COMPLETED (30개 교체) | 거부, 그대로 | — | 기록 |
| `FAILED` | → **PENDING** | 그대로 + 알림 | 그대로 → 재시도 | → COMPLETED (늦게 온 결과도 저장) | 거부 | — | 기록 |

상태가 바뀌는 곳은 **세 군데뿐**: 202 → PENDING, 7.7 200 → COMPLETED, 타임아웃 → FAILED. **오류 응답은 상태를 바꾸지 않는다.**

PENDING이 10분을 넘기면 FAILED로 가기 전에 **같은 번호로 최대 2회 재전송**한다(⑤). 재전송은 상태를 바꾸지 않는다 — PENDING 그대로다.

### 3.4 사건별 규칙 (자세히)

**① 7.6 을 보낼 때 (새 번호)** — `last_changed_at`이 있고 (지금 − `last_changed_at` ≥ 1h **또는** 지금 − `window_started_at` ≥ 6h)이면:
```
source_version += 1 ; 저장(커밋)                          # 요청보다 먼저 — 번호는 "보낸 순번"
sent_at = now() ; 보내기 직전의 last_changed_at 값을 기억
POST 7.6 (sourceVersion = source_version)
```
**재시도는 +1 하지 않는다 (09-27).** 7.6이 접수되지 못해도(500·503·연결 실패·타임아웃) 번호는 그대로 두고 `next_retry_at`(백오프, 503은 `Retry-After`)을 적어 **같은 번호로** 다시 보낸다 — 202를 받고 결과가 안 오는 경우(⑤)와 같은 `retry_count`로 센다. 번호를 올리면 AI 쪽에 같은 요청이 두 벌 생기고 이미 만든 결과를 버리고 다시 분석하게 된다. 재시도를 기다리는 동안 새 수정이 들어오면 옛 번호는 버리고 새 번호가 나간다(BE 계획 5-2).

**② 202 를 받았을 때 (→ PENDING, 조건부)**
```
if analyzed_source_version < source_version:            # 아직 이 번호의 결과가 없을 때만 (빠른 콜백 대비)
    profile_status = PENDING
    pending_since = now()
if last_changed_at == 보내기 직전에 읽은 값:              # 그 사이 수정이 없었을 때만 비움
    last_changed_at = NULL; window_started_at = NULL
```
첫 조건이 없으면 **7.7이 202보다 먼저 처리되는 경우**(v1은 콜백까지 50ms — §4 그림 5) COMPLETED를 PENDING으로 덮어쓰고 10분 뒤 FAILED로 오판한다. 둘째 조건이 없으면 분석 중에 들어온 수정이 대기열에서 사라진다(계약 §7.6 "갱신 잠금").

**③ 7.7 을 받았을 때 (→ COMPLETED, 최신일 때만)**
```
if body.recipientUserId != path.recipientUserId: 400 RECIPIENT_ID_MISMATCH
if body.sourceVersion < analyzed_source_version:   409 STALE_SOURCE_VERSION   # 낡은 결과가 최신을 덮는 것만 거부
else:                                                                          # 같으면 재전송 — 그대로 다시 저장, 200
    recommended_product_ids = body.recommendedProductIds
    analyzed_source_version = body.sourceVersion
    if body.sourceVersion == source_version:        # 마지막으로 보낸 번호의 결과 = 최신
        profile_status = COMPLETED; pending_since = NULL
    # 더 낮은 번호면 새 요청이 진행 중 — 30개는 갱신하되 PENDING 유지 (그 결과가 오면 COMPLETED)
    200
```
**최신 여부는 번호 비교 하나로 안다**: `analyzed_source_version == source_version`. 분석 중 사용자가 수정해 다음 번호가 이미 나갔더라도 이번 결과는 **저장한다**(저장본보다는 새로우므로 화면은 바로 이걸 쓴다) — 상태만 PENDING으로 두어 "더 새 결과가 오는 중"임을 나타낸다.

**④ PENDING 타임아웃 (→ FAILED, BE 판정)**
```
if profile_status == PENDING and now() − pending_since ≥ 10분 and last_changed_at is NULL:
    profile_status = FAILED
```
`last_changed_at`이 있으면(대기 중인 수정이 있으면) FAILED로 바꾸지 않는다 — 어차피 다음 7.6이 나가므로, 정상 대기 구간을 실패로 표시하지 않기 위해. 10분은 제안값 — v1 처리는 초 단위라 넉넉하다(나중에 모델이 붙으면 처리 기한보다 길게 다시 정한다).

**⑤ PENDING이 오래 갈 때 자동 재전송 (09-25 합의)** — AI는 실패해도 아무것도 보내지 않으므로, 재전송이 없으면 사용자가 취향을 다시 고칠 때까지 그 수신자는 계속 실패 상태로 남는다. 그래서 BE가 이렇게 한다.

```
if profile_status == PENDING and now() − pending_since ≥ 10분 and retry_count < 2:
    retry_count += 1                       # 다중 인스턴스면 조건부 원자 갱신 (… WHERE retry_count < 2)
    POST 7.6 (sourceVersion = source_version)      # ← 같은 번호. 본문도 최초와 같아야 한다
retry_count = 0    # 새 번호로 요청할 때 · 7.7 결과를 저장했을 때
```

**같은 번호로 보내는 이유**(AI 쪽 사정) — AI의 실행 기록은 `(수신자, sourceVersion)` 한 행이라 같은 번호면 그 행에 모이고, **이미 결과가 있으면 재분석 없이 저장해 둔 콜백 본문을 그대로 다시 보낸다**. 새 번호로 보내면 매번 전체 재분석이라 그 사이 카탈로그가 바뀌면 같은 요청인데 추천 30개가 달라진다.

**AI가 같은 번호를 받으면**(접수 단계에서 판정 — `intake.decide()`):

| AI 쪽 상태 | 하는 일 |
|---|---|
| 분석 중(`RUNNING`) | 아무것도 하지 않음 — 분석이 두 벌 돌지 않게. 원래 실행이 끝나면 콜백이 간다 |
| 결과 있음(`RESULT_READY`·`DELIVERED`·`SUPERSEDED`) | **재분석 없이 7.7 재전송** (최초와 같은 30개) |
| 실패(`FAILED`)·죽은 `RUNNING`(5분 초과) | 다시 분석 |
| 같은 번호인데 **본문이 다름** | 다시 분석 — 옛 결과 재전송은 틀린 답이 된다. 그래서 재전송 본문은 최초와 같아야 한다 |

어느 경우든 응답은 `202 PENDING`이다.

**⑥ 사용자가 비선호를 바꿀 때** — `last_changed_at = now()`, `window_started_at`이 비어 있으면 `now()`. **번호는 올리지 않고 `profile_status`도 건드리지 않는다**(PENDING이면 PENDING 유지, COMPLETED면 옛 30개 계속 노출). 몇 번을 고치든 다음 7.6 한 번이 그 시점의 최신 상태를 실어 간다.

### 3.5 `sourceVersion` 규칙 (한 줄씩)

- **7.6을 보내기 직전에 +1 하고 저장한다** — 수정할 때마다가 아니다. 사용자가 몇 번을 고치든 다음 7.6 한 번이 한 번호를 쓴다. (⚠ 계약 원문 "변경마다 +1"과 다름 — 09-22 결정. AI 쪽은 번호를 비교만 하므로 영향 없음.)
- 번호는 **요청 순번**이다. 새 수정으로 새 요청을 만들 때만 +1. **재시도**(접수 실패든 결과 미도달이든)는 +1 하지 않는다 — 같은 번호 그대로(09-27 BE 계획 5-1).
- AI는 **절대 바꾸지 않고 그대로 돌려준다**(202·7.7 모두).
- 7.7의 판정 두 가지: `body.sourceVersion < analyzed` → 409(폐기) · `body.sourceVersion == source_version` → **최신, COMPLETED** · 그 사이 → 저장하되 PENDING 유지.
- **"지금 결과가 최신인가" = `analyzed_source_version == source_version`.** `last_changed_at`은 디바운스(다음 7.6을 언제 보낼지)에만 쓴다.
- 같은 (수신자, 번호)로 7.6이 다시 오는 것은 **정상 흐름이다**(09-25 재전송). AI는 실행 기록을 보고 갈라진다 — 기록이 없으면(접수 실패였다) 분석하고, 결과가 있으면 **재분석 없이 저장된 본문을 재전송**하며, 같은 번호인데 본문이 다르면 다시 분석한다(`intake.decide()` · §3.4 ⑤).

### 3.6 오류·**재시도**(접수 실패) 정책과 상태

7.6 응답별 BE 동작 — 09-27 BE 계획 5-1 오류표 그대로. 접수 실패도 202 뒤 결과 미도달도 **같은 번호·같은 `retry_count`**로 센다.

| BE가 본 것 | BE 동작 | `profile_status` | `next_retry_at` |
|---|---|---|---|
| 연결 실패·응답 타임아웃 | 백오프 뒤 **같은 번호** 재시도 (`retry_count` +1) | 그대로 | 백오프 |
| **503** `SERVICE_UNAVAILABLE` | **`Retry-After`(초) 우선**, 없으면 백오프 → 같은 번호 재시도 | 그대로 | `Retry-After` |
| **500** | 백오프 뒤 같은 번호 재시도 | 그대로 | 백오프 |
| **400 / 401** | 재시도 없음 — 계약·토큰 오류 로그. 고친 뒤 새 번호 | 그대로 | NULL |
| PENDING 10분 타임아웃 | 같은 번호 재시도(⑤) — 새 수정이 있으면 대신 새 번호 | PENDING 유지 | 기록 |
| `retry_count` ≥ 2 인데 또 실패 | **FAILED** (`pending_since`·`next_retry_at` NULL) | FAILED | NULL |
| **202** | — | → PENDING (조건부) | NULL |

백오프 값은 BE가 정한다(미정). AI가 503에 싣는 `Retry-After`는 300초다(`PROFILING_RETRY_AFTER_S`). 디바운스(1시간)와는 목적이 다르다 — 디바운스는 사용자가 계속 고치는 동안 기다리는 시간이다. 6시간 상한은 그대로.

### 3.7 경합 세 가지

| 경합 | 무슨 일 | 막는 방법 |
|---|---|---|
| **빠른 콜백** | v1은 202 뒤 50ms 안에 7.7이 온다. BE가 202 처리(PENDING 저장)를 끝내기 전에 7.7이 COMPLETED를 저장 → 그다음 PENDING이 덮어씀 → 10분 뒤 FAILED 오판 | §3.4 ② 조건부 PENDING (`analyzed_source_version < 보낸 버전`일 때만) |
| **분석 중 수정** | v5 분석 중 사용자가 수정. 7.7(v5)이 오면 저장하지만 그 수정은 반영 안 됨 | §3.4 ② `last_changed_at` 조건부 비움 → 수정이 대기열에 남아 다음 번호(v6)로 7.6. v6이 이미 나갔으면 7.7(v5)은 저장만 하고 PENDING 유지(§3.4 ③) |
| **늦은 옛 콜백** | v6 저장 뒤 v5 콜백(재전송)이 도착 | §3.4 ③ 409 → AI가 폐기 |

---

## 4. 시퀀스 다이어그램

PNG는 `docs/assets/be-seq/v1/`(`build_be_sequences.py`가 이 문서의 mermaid 블록을 그대로 그림). GitHub에서는 아래 mermaid가 바로 렌더된다.

### 4.1 성공 — 상품 목록 적재 → 7.6 → 7.7 → 화면

![성공](assets/be-seq/v1/01-success.png)

<!-- fig: 01-success -->
```mermaid
sequenceDiagram
  autonumber
  participant U as 사용자(수신자·발신자)
  participant BE as Backend
  participant AI as AI 프로파일링
  participant DB as AI DB
  Note over BE,AI: 사전 1회 (v1 수동) — BE가 상품 목록 파일 전달, AI가 적재
  BE-->>AI: 상품 목록 파일 (productId = products.id, categoryId, views)
  AI->>AI: 계약 점검 · 저장 → 활성 카탈로그
  U->>BE: 비선호 카테고리 저장
  BE->>BE: last_changed_at 기록 (번호는 아직)
  Note over BE: 마지막 변경 후 1h (최대 6h)
  BE->>BE: source_version += 1 → v (요청보다 먼저 저장)
  BE->>AI: POST 7.6 {recipientUserId, sourceVersion: v, dislikedCategories[]}
  AI->>DB: profile_runs(v) RUNNING
  AI-->>BE: 202 {profileStatus: PENDING, sourceVersion: v}
  BE->>BE: analyzed < v 이면 PENDING · last_changed_at 조건부 비움
  AI->>AI: 비선호 제외 → 조회수순 30개
  AI->>DB: profile_runs(v) RESULT_READY + 콜백 본문 · recipient_profiles upsert
  AI->>BE: POST 7.7 {recipientUserId, sourceVersion: v, profileStatus: COMPLETED, recommendedProductIds[≤30]}
  BE->>BE: v ≥ analyzed → 30개 저장 · analyzed=v · v == source_version → COMPLETED (최신)
  BE-->>AI: 200 {profileStatus: COMPLETED}
  AI->>DB: profile_runs(v) DELIVERED
  U->>BE: 상품 목록 (sort=AI_RECOMMENDED, recipientUserId)
  BE-->>U: 30개 우선 노출 (비선호 카테고리는 뒤로)
```

### 4.2 실패 — 7.6 이 503 (AI에 활성 카탈로그 없음)

![503](assets/be-seq/v1/02-503-no-catalog.png)

<!-- fig: 02-503-no-catalog -->
```mermaid
sequenceDiagram
  autonumber
  participant BE as Backend
  participant AI as AI 프로파일링
  BE->>AI: POST 7.6 (sourceVersion: v)
  AI-->>BE: 503 {code: SERVICE_UNAVAILABLE, message: "활성 카탈로그가 없습니다."}  Retry-After: 300
  Note over BE: profile_status 그대로(이전 값) · next_retry_at = now + 300 · retry_count 0
  Note over AI: 운영자: 상품 목록 파일 적재 → 활성
  BE->>AI: POST 7.6 (같은 번호 v) — Retry-After 뒤 재시도 1 (retry_count 1)
  Note over BE,AI: 접수가 안 됐으니 AI에는 기록이 없다 → 같은 번호가 와도 새로 분석한다(§3.4 ①). 2회를 다 쓰면 FAILED
  AI-->>BE: 202 PENDING
  AI->>BE: POST 7.7 (v)
  BE-->>AI: 200 → COMPLETED
```

### 4.3 실패 — 순서 역전 → 7.7 이 409

![409](assets/be-seq/v1/03-409-stale.png)

<!-- fig: 03-409-stale -->
```mermaid
sequenceDiagram
  autonumber
  participant U as 사용자
  participant BE as Backend
  participant AI as AI 프로파일링
  BE->>BE: source_version 4 → 5 (저장)
  BE->>AI: POST 7.6 (v5)
  AI-->>BE: 202 → PENDING (analyzed 없음 < 5)
  U->>BE: 비선호 다시 수정 → last_changed_at 재기록 (번호는 그대로 5)
  Note over BE: 1h 뒤 — v5 결과가 아직 안 왔어도 다음 번호로 보냄 (v3처럼 처리가 길면 생김)
  BE->>BE: source_version 5 → 6 (저장)
  BE->>AI: POST 7.6 (v6)
  AI-->>BE: 202 → PENDING 유지
  AI->>BE: POST 7.7 (v5) — 먼저 끝난 쪽
  BE->>BE: 5 ≥ analyzed(없음) → 30개 저장, analyzed=5 · 5 < source_version 6 → PENDING 유지
  BE-->>AI: 200
  AI->>BE: POST 7.7 (v6)
  BE->>BE: 6 ≥ 5 → 저장, analyzed=6 · 6 == source_version → COMPLETED (최신)
  BE-->>AI: 200
  Note over AI,BE: 그 뒤 v5 콜백이 재전송으로 늦게 도착하면
  AI->>BE: POST 7.7 (v5)
  BE-->>AI: 409 {code: STALE_SOURCE_VERSION}
  Note over AI: profile_runs(v5) SUPERSEDED · 재시도 없음
```

### 4.4 실패 — AI 처리 실패(침묵) → PENDING 타임아웃 → FAILED → 자동 재전송

![timeout](assets/be-seq/v1/04-silent-fail-timeout.png)

<!-- fig: 04-silent-fail-timeout -->
```mermaid
sequenceDiagram
  autonumber
  participant BE as Backend
  participant AI as AI 프로파일링
  participant DB as AI DB
  BE->>AI: POST 7.6 (v)
  AI->>DB: profile_runs(v) RUNNING
  AI-->>BE: 202 → PENDING, pending_since 기록
  AI--xDB: 결과 저장 실패 (예: DB 연결 끊김)
  Note over AI: profile_runs(v) FAILED {code: STORE_FAILED} · 7.7 보내지 않음 (AI는 침묵)
  Note over BE: 10분 경과 · last_changed_at 비어 있음
  BE->>BE: profile_status = FAILED
  Note over BE: 재전송(09-25): retry_count < 2 면 같은 번호로 다시 보낸다
  BE->>AI: POST 7.6 (같은 v)
  AI->>DB: get_run(수신자, v) → FAILED 이므로 다시 분석 (attempt=2)
  AI-->>BE: 202 → PENDING
  AI->>DB: RESULT_READY
  AI->>BE: POST 7.7 (v)
  BE-->>AI: 200 → COMPLETED
```

### 4.5 실패 — 빠른 콜백 경합 (v1에서 실제로 일어남)

![race](assets/be-seq/v1/05-fast-callback-race.png)

<!-- fig: 05-fast-callback-race -->
```mermaid
sequenceDiagram
  autonumber
  participant BE as Backend
  participant AI as AI 프로파일링
  BE->>AI: POST 7.6 (v)
  AI-->>BE: 202 (BE 쪽 트랜잭션은 아직 열려 있음)
  AI->>BE: POST 7.7 (v) — 202 뒤 50ms
  BE->>BE: v ≥ analyzed → 저장, COMPLETED(v)
  BE-->>AI: 200
  BE->>BE: 202 처리 계속 — PENDING으로 바꾸려 함
  alt 조건부 갱신: analyzed(v) < source_version(v) ? → 아니오
    Note over BE: COMPLETED 유지 ✓
  else 무조건 갱신
    Note over BE: PENDING으로 덮음 ✗ → 10분 뒤 FAILED 오판
  end
```

### 4.6 재전송 — 10분 PENDING → 같은 번호 7.6 → AI는 재분석 없이 재전송 (09-25)

![재전송](assets/be-seq/v1/06-retry-resend.png)

<!-- fig: 06-retry-resend -->
```mermaid
sequenceDiagram
  autonumber
  participant BE as Backend
  participant AI as AI 프로파일링
  participant DB as AI DB
  BE->>AI: POST 7.6 (sourceVersion v) — 최초
  AI-->>BE: 202 PENDING · retry_count 0
  AI->>DB: profile_runs(v) RUNNING → RESULT_READY + 콜백 본문
  AI--xBE: POST 7.7 (v) — 유실 또는 5xx
  Note over BE: PENDING 10분 경과 · retry_count(0) < 2
  BE->>BE: retry_count = 1 (조건부 원자 갱신)
  BE->>AI: POST 7.6 (같은 v · 같은 본문)
  AI->>DB: get_run(수신자, v) → RESULT_READY · input_hash 같음
  AI-->>BE: 202 PENDING (분석은 돌리지 않는다)
  AI->>BE: POST 7.7 (v) — 저장해 둔 그 30개 그대로
  BE->>BE: 저장 · COMPLETED · retry_count = 0
  BE-->>AI: 200
  AI->>DB: profile_runs(v) DELIVERED · callback_attempts 2 · attempt 1
```

분석이 아직 돌고 있으면(`RUNNING`) AI는 **아무것도 하지 않고** 202만 준다 — 원래 실행이 끝나면 콜백이 간다. 실패했거나(`FAILED`) 죽은 `RUNNING`(5분 초과)이면 다시 분석한다.

---

## 5. BE에 확인·요청하는 것

### 5.1 재전송 정책 (09-25 BE 제안에 대한 회신)

**확인해 준 것** — `retry_count`는 BE `profiles` 테이블의 열이고 **AI 표에는 영향이 없다.** AI는 이미 `profile_runs.attempt`(분석 재실행)와 `callback_attempts`(7.7 시도)로 센다. 같은 `sourceVersion` 재전송에 동의하며, AI는 이미 결과가 있으면 재분석 없이 **최초와 같은 본문**을 재전송한다(§3.4 ⑤).

| # | 확인 요청 | 왜 |
|---|---|---|
| 1 | 재전송 직전 `last_changed_at`이 비어 있지 않으면(대기 중인 수정이 있으면) 재전송 대신 **다음 7.6(새 번호)**으로 가는가? | 낡은 입력으로 재시도하지 않기 위해. 타임아웃 규칙(§3.4 ④)과 같은 조건 |
| 2 | **재전송 본문은 최초와 같아야 한다** | 같은 번호인데 비선호 목록이 다르면 AI가 `input_hash`로 감지해 재분석한다. 입력이 바뀐 것이면 새 번호로 보내야 한다 |
| 3 | 2회를 다 쓴 뒤 상태는? `FAILED` 유지 + 사용자가 다시 고칠 때만 새 7.6인가 | AI는 그 뒤에도 아무것도 보내지 않는다(침묵) |
| 4 | 다중 인스턴스면 증가를 **조건부 원자 갱신**으로: `UPDATE … SET retry_count = retry_count + 1 WHERE recipient_user_id = ? AND retry_count < 2` | 스케줄러 두 대가 같은 타임아웃을 보면 2회가 한꺼번에 나갈 수 있다 |
| 5 | ~~재시도와 재전송을 구분해 주세요~~ **09-27 BE 계획 5-1로 정리됨** — 접수 실패도 결과 미도달도 **같은 번호**, `retry_count` 하나. 우리 문서를 그 모델로 고쳤다(§0 용어·§3.4 ①·§3.5·§3.6·§4.2) | AI는 같은 번호를 실행 기록으로 가르므로 어느 쪽이든 안전하다 |
| 6 | (선택) 10분 → 3분 | v1 처리는 밀리초 단위라 복구가 빨라진다. 모델이 붙는 v3에서 다시 정하면 된다 |

### 5.2 먼저 와야 연동이 완성되는 것

| # | 요청 | 지금 상태 |
|---|---|---|
| 1 | ~~Backend 상품·카테고리 ID 회신~~ **받음 (09-25)** | xlsx 2종으로 회신. 상품 4,231/4,231 · 카테고리 67/67 반영 완료. 7.7로 나가는 번호가 이제 Backend 번호다 |
| 2 | ~~재고·조회수~~ **받음 (09-25)** · 확인 하나 | 회신의 재고가 **전건 100**이라 전부 `available`로 저장했다. 실제 재고가 아니라 MVP 기본값이면, 실값이 생길 때 다시 보내 주면 `--metrics`로 덮는다. 조회수(102~49,989)는 그대로 정렬에 쓴다 |
| 3 | 7.7 태그 포함 여부 · 7.9 조회수 필드명 · 비선호 제외 vs 감점 | 09-22 이후 미결 |


---

## 6. 부록 — 오류 코드 전체 (2026-09-27)

BE 계획 5-1은 AI 응답을 "오류 유형"으로 나눠 재시도 여부를 정한다. 그 판정에 쓸 수 있도록 **AI가 내는 코드 전부**를 한곳에 모았다. 세 층이다 — ① 7.6 응답(BE가 받는 것), ② 7.7 응답을 AI가 어떻게 처리하는지(BE가 주는 것), ③ AI 실행 기록의 내부 코드(로그·DB에서만 보인다).

### 6.1 7.6 — AI가 돌려주는 HTTP 응답

봉투는 항상 `{"message": "...", "error": {"code": "..."}}`이고 `code`는 아래 값뿐이다.

| HTTP | `error.code` | 언제 | `message` | BE 재시도 (계획 5-1) |
|---|---|---|---|---|
| 202 | — | 접수. `data.profileStatus`는 항상 `PENDING` | `프로파일 분석이 시작되었습니다.` | — |
| 400 | `INVALID_REQUEST` | 본문이 JSON이 아님 · 필수 키 누락 · 타입·범위 오류 · 목록 상한(비선호 5·리뷰 10). `message`에 **필드 경로와 사유** | `요청 본문이 JSON이 아닙니다.` / `요청 형식이 올바르지 않습니다: <경로> — <사유>` | 없음 — 계약 오류 로그 |
| 401 | `UNAUTHORIZED` | `Authorization` 없음·`Bearer ` 아님 / 토큰 값 다름 | `서비스 토큰이 없습니다.` / `서비스 토큰이 올바르지 않습니다.` | 없음 — 토큰 설정 |
| 404 | `NOT_FOUND` | 경로 오타 | `Not Found` | 없음 — 주소 확인 |
| 405 | `INTERNAL_SERVER_ERROR` | 잘못된 메서드(GET 등). 코드 이름이 상황과 맞지 않는다 — 알려진 한계 | `Method Not Allowed` | 없음 — 메서드 확인 |
| 503 | `SERVICE_UNAVAILABLE` | 활성 카탈로그 없음. **헤더 `Retry-After: 300`** | `활성 카탈로그가 없습니다.` | `Retry-After` 뒤 **같은 번호** |
| 503 | `SERVICE_UNAVAILABLE` | 접수 대기열 가득(`QUEUE_MAX` 200) 또는 종료 중(09-28). **헤더 `Retry-After: 30`** — 기다리지 않고 바로 거절 | `접수 대기열이 가득 찼습니다.` | `Retry-After` 뒤 **같은 번호** |
| 500 | `INTERNAL_SERVER_ERROR` | 잡히지 않은 예외 | `서버 오류가 발생했습니다.` | 백오프 뒤 **같은 번호** |

- 토큰과 본문이 둘 다 틀리면 **401이 먼저**다(의존성이 본문보다 먼저 풀린다).
- 계약에 없는 필드는 오류가 **아니다** — 202 접수, AI 로그에 `CONTRACT_7_6_UNKNOWN_FIELD` 경고만.
- 202 뒤에 AI가 실패하면 **아무것도 보내지 않는다**(FAILED 콜백 없음). BE는 PENDING 타임아웃으로 안다(§3.4 ④).

### 6.2 7.7 — BE 응답을 AI가 처리하는 규칙

| BE 응답 | AI 실행 기록 `status` | `error.code` | AI 동작 |
|---|---|---|---|
| 200 | `DELIVERED` | — | 끝 |
| 409 `STALE_SOURCE_VERSION` | `SUPERSEDED` | `CALLBACK_STALE` | 폐기. 정상 경로 |
| 400 · 401 · 403 | `FAILED` | `CONTRACT_7_7_REJECTED` | 재시도 없음. `error.reason`에 `"400 INVALID_REQUEST"`처럼 BE 코드가 남는다 |
| 5xx · 타임아웃(5초) · 연결 실패 | `RESULT_READY` | `CALLBACK_UNREACHABLE` | **즉시 3회**(0.5초·2초 뒤) → 그래도 실패면 보관. 같은 번호 재요청 때 재분석 없이 재전송 |

빈 배열(`recommendedProductIds: []`)은 오류가 아니다 — §2.

### 6.3 AI 실행 기록 `profile_runs.error.code` — 로그·DB에서만 보인다

HTTP로 나가지 않는다. `select status, error from ai_profile.profile_runs where recipient_user_id = ? and source_version = ?`로 보고, 로그 키는 `recipient=<id> source_version=<n>`이다.

| 코드 | 단계 | 뜻 | 겉으로 보이는 것 |
|---|---|---|---|
| `CONTRACT_7_6_UNKNOWN_FIELD` | 접수 | 7.6 본문에 계약 밖 필드 — 무시하고 진행 | 202. 로그 경고만, 기록에는 안 남는다 |
| `NO_ACTIVE_CATALOG` | 처리 | 백그라운드에서 활성 카탈로그가 사라짐(접수 때는 있었다) | `FAILED`, 콜백 없음 |
| `STORE_FAILED` | 처리 | 실행 기록·프로필 저장 실패(DB) | `FAILED`, 콜백 없음 |
| `PIPELINE_ERROR` | 처리·복구 | 그 밖의 예외 · 시작 시 정리된 끊긴 RUNNING(`reason="프로세스가 끝나 실행 기록만 남음 — 시작 시 정리"`) | `FAILED`, 콜백 없음 |
| `CALLBACK_STALE` | 콜백 | 7.7 409 | `SUPERSEDED` |
| `CALLBACK_UNREACHABLE` | 콜백 | 7.7 5xx·타임아웃·연결 실패(즉시 3회 뒤에도) | `RESULT_READY` · `/health`의 `store.undelivered` +1 |
| `CONTRACT_7_7_REJECTED` | 콜백 | 7.7 4xx | `FAILED` |
| `CONTRACT_7_9_SCHEMA` | 카탈로그 CLI | 7.9 export 필드 불일치 — 저장 거부 | `fetch_export.py` 종료 코드 1 |

`FAILED`인 행은 BE가 같은 번호로 다시 요청하면 **다시 분석**한다(`intake.decide()` → analyze). `RESULT_READY`인 행은 재분석 없이 재전송한다. 코드 값의 정본은 `src/profiling/types.py`의 `ErrorCode`(8종)다.
