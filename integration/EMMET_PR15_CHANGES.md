# PR15 병합 후 통합 변경 기록

기준은 [PR #15](https://github.com/100-hours-a-week/KTB4-12th-AI/pull/15)가 squash 병합된 `develop`의 `bfce5219ef622a1634569e3b92b879d10e51e9b2`다. 병합 전 PR head는 `21b85a351fe2cc344398febb6cd7fd418e77e54a`다. 동결 후보 `f8821e6b593b7cc751e064ea46ca6c9202da6bde`는 보존하고 새 브랜치에서 최신 소스와 파일별로 비교해 통합했다. squash 이전 커밋 관계만으로 동일성을 판단하지 않았다.

## 최신 에멧 코드에서 보존한 동작

취향 해석, 태그 3축과 `tag3disliked`, 명시적 비선호, 소분류 편향, MMR·중복 모델 제한, 후보 최대 30개와 개인 프로필은 Profiler의 책임이다. Chat 표시 최대 3개 및 요청한 더 적은 수와는 별도 계약이다.

PR15에 반영된 S0 명시적 비선호 제외는 **기존 점수·순위를 계산한 뒤 pool을 자르는 단계**에서 적용한다. category penalty와 기존 rule 값도 유지한다. `selection.py`, `tagging.py`, `vocab.py`는 develop과 동일하다. pipeline의 오류 처리, store·intake·backend·supervisor 로그 및 ASGI 500 로그의 개인정보 차단 수정도 보존했다. 통합을 위해 기존 397개 단위 회귀의 기대값을 바꾸지 않았다.

마이그레이션 `0001`–`0011`과 TSV 어휘를 수정하지 않았다. 이미 `0008` 이상인 DB에는 수정된 `0007`이 소급되지 않는다. 기존 DB downgrade나 역할 자격 증명 변경은 하지 않는다. 검증은 후보 전용 새 DB에서 수행하며 결과 범위는 [릴리스 검토표](RELEASE_READINESS.md)에 기록한다. 과거 `tag4→tag3` 설정 변화와 달리 **현재 develop 기본값은 `tag3disliked`**다.

## 이번에 적용한 Profiler 변경

| 변경 위치 | 원래 동작·재현 | 변경 이유와 현재 동작 | 회귀·호환성 영향 |
|---|---|---|---|
| `embedding.py` | 극단적인 비영 float32 벡터의 norm이 overflow/underflow하고 NaN·0·잘못된 shape를 막지 못함 | 정상 float32 결과는 그대로 두고 극단값만 float64 scaling. invalid 행·잘못된 query 차원은 값 없이 거부 | hardening의 극단값·invalid·ordinary exact 및 기존 embedding 시험. 정상 벡터 의미 유지, 잘못된 입력은 실패 |
| `settings.py`, `main.py`, 새 `search_api.py` | file/db 카탈로그만 지원 | `search_http`를 추가해 상품·벡터·query encoder를 같은 세대에 바인딩. 기존 file/db 유지 | 모델·revision·차원·정규화·지문·세대 불일치, pagination, binding·가시성 회귀. shared 장애는 로컬 카탈로그로 대체하지 않음 |
| `pipeline.py`, `search_api.py` | 공용 카탈로그에서 비선호 분류가 해석되지 않아도 0개 매칭으로 진행 가능 | 같은 세대의 전체 `categoryTaxonomy`로 모델 호출 전 ID/이름 확인. shared 어댑터만 선택적 검증 hook 제공 | 상품이 없는 유효 분류는 허용하고 매핑 불가 분류는 실패. native file/db와 순수 ranking에는 새 분류 정책을 추가하지 않음 |
| `main.py` health | 재바인딩 후에도 부팅 당시 embedding_status가 남을 수 있음 | 보유 snapshot 하나에서 catalog와 embedding 상태를 함께 읽음 | managed health·rebind 회귀. 기존 `catalog.active`, `productTags`, `store.connected` 응답 유지 |
| `intake.py`, `integration/profile_callback.py` | 저장된 콜백을 재전송할 때 삭제된 상품이 포함될 수 있음 | 전송 직전 현재 Search 가시성 검사. 차단 시 원문·sourceVersion·이미 기록한 전송 상태 보존 | 첫 전송 차단·재시도 중 삭제·성공 재전송 회귀. BE에 도달하지 않은 시도는 증가시키지 않으며 과거 BE 결과를 회수하지 않음 |
| 새 `profile_read.py`, `main.py` router | 개인 프로필 읽기 API 없음 | service token에 더해 grant/caller/recipient/sourceVersion·만료·철회를 검사하고 파생 태그만 반환 | 401/403/200/409·철회 회귀. 원문·리뷰·pool 제외. BE grant 발행 계약이 없어 외부 Chat 요청에는 자동 연결하지 않음 |
| `__init__.py` | pure vocab import도 API/settings 의존성을 즉시 로드 | 기존 공개 속성은 lazy export해 Search에서 pure vocabulary 사용 | import·공용 상품 태그·native CPU 부팅 시험. 공개 import 이름 유지 |

`integration/profiler_generation.py`의 `GuardedEmbedding`과 `profiler_boundary.py`의 `RequiredStore`는 통합 실행의 필수 임베딩 실패를 기록해 성공 결과 저장을 막는다. **Profiler 단독 알고리즘의 원래 fallback은 그대로**다. 이 차이는 `test_required_embedding_failure_blocks_even_if_algorithm_falls_back`와 `test_embedding_failure_is_not_lost_when_algorithm_degrades`로 검증한다. 실패를 숨긴 부분 개인화를 통합 성공으로 보내지 않기 위한 경계이며 native 경로의 기본 정책을 바꾼 것이 아니다.

재바인딩은 큐뿐 아니라 DB의 `RUNNING/RESULT_READY/FAILED`도 확인한다. 확인 실패나 미완료 행이 있으면 전환을 보류하고 기존 행을 수정·삭제하지 않는다. 승인된 작업은 기존 상품·벡터 바인딩을 유지한다. 전체 서비스의 자동 무중단 전환 조정기를 구현한 것은 아니다.

## 동결 후보와 달라진 정책: 이번 통합에서 제외

다음은 동결 후보에 있던 변경 중 별도 정책 합의가 필요한 항목이다. 새 develop 기본 동작을 채택했으며 과거의 결함 재현 통과 수치를 이번 후보의 품질 근거로 재사용하지 않는다.

| 항목 | 동결 후보 | 이번 후보의 develop 동작 |
|---|---|---|
| 장문 g5 처리 | 잘리면 `TEXT_LENGTH_REQUIRES_REVIEW` 실패 | 마스킹 후 앞 1,000자 사용. 400 응답이나 새로운 실패 상태를 추가하지 않음 |
| 저평점 텍스트 | `tag3disliked`에서 저평점 글만 있어도 LLM 호출 가능 | 기존 취향·고평점 조건 유지. 저평점 글만으로 추가 호출하지 않음 |
| native 임베딩 장애 | selection에서 오류 전파 | 기존 순위 fallback과 rule 유지. 통합 실행의 별도 필수 임베딩 보호는 위 설명대로 유지 |
| 미등록 ASCII 브랜드·짧은 검색어 | 경계 matcher와 한 글자 query 보존 추가 | 원래 부분 일치와 한 글자 처리 유지. 알려진 `꿀` 등의 기존 확장은 유지 |
| 해석 불가 비선호 태그 | `UNRESOLVED_DISLIKED_TAG` 실패 | 원래 matcher 의미 유지. shared 분류 taxonomy 검증과 구분 |
| 태그 지문 | 코드 바이트+RULE+TSV hash | upstream RULE+TSV hash 유지. Search는 브랜드 집합 hash와 태그 내용도 재계산해 검사 |
| S0 명시적 비선호 | 별도 변경한 순위/제외 순서 | PR15의 순위 계산 후 제외 및 원래 점수·rule 보존 |

추가한 `test_integration_hardening.py` 37개는 수치 안정성·shared 경계와 이 정책 선택을 검증한다. 기존 unit 파일은 모두 develop 그대로다. 코드 revision은 별도 source manifest로 추적하고 upstream 태그 hash에 현재 코드의 hash를 섞어 넣지 않는다. 옛 코드 기반 태그 지문을 새 지문으로 이름만 바꿔 재사용하지 않는다.

저장 프로필에는 추론 당시 vocabulary 지문이 없으므로 읽기 응답은 `catalogVersion`과 `tagTaxonomy:null`을 반환한다. 현재 지문을 과거 데이터에 소급하지 않는다. opt-in Chat reader는 미검증 선호 태그를 보충하지 않고 미검증 비선호 태그는 503으로 차단한다. 기존 DB backfill은 없다.

## PC 하네스와 외부 계약

`/home/johnas/product-agent-lab`의 `main` `1ea3cdefe3137503be6725651452f0a25f0a1892`와 당시 미커밋 스냅샷의 `stable-v4.12`를 기준으로 한다. 원본 변경 없이 Chat 도구 실행과 상태 구조를 보존하고 공용 상품 API를 어댑터로 연결했다.

동결 후보의 optional tool 인자 `strict:false`와 공개 조건/서버 제약 구분을 유지한다. optional 필드 누락과 명시적 null 해제를 구분하고 비공개 제약 값은 모델 응답에 노출하지 않는다. 의미 판단을 대체하는 문장별 regex, 개수 강제 채움, 자동 가격 완화는 추가하지 않았다. 과거 합성 LLM 실행은 이전 소스의 증거이며 이번 최종 tree의 LLM 검증은 아니다.

AI–BE HTTP/SSE·콜백, `productId/reason`, 최대 3개와 요청한 더 적은 수, 멱등성·취소·소유권·예산 예약은 유지한다. **BE400의 하루 10턴·취향 수정 3크레딧은 BE 소유의 미구현 계획**이며 AI에 할당량·과금 규칙을 추가하지 않았다.
