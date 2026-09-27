# BE ↔ AI 연동 필드표 — 색인 (v1 · v2 · v3)

BE 팀에 전달하는 연동 문서. 버전마다 **그 버전에서 필요한 것만** 적었다. 지금 연결하는 것은 v1.

| 판 | 문서 | 범위 | 7.6 입력 | 상품 목록 | 7.7 | 상태 |
|---|---|---|---|---|---|---|
| **v1** | [BE_연동_필드표_v1.md](BE_연동_필드표_v1.md) | 비선호 카테고리 반영 (MVP) | `recipientUserId` · `sourceVersion` · `dislikedCategories` **세 필드** | BE가 파일로 전달, AI 수동 적재 | 상품 ID 30개 (태그 없음) | **구현 완료, BE 연결 대기** |
| v2 | [BE_연동_필드표_v2.md](BE_연동_필드표_v2.md) | + 상품 export API | 동일 | **7.9 API**를 AI CLI가 호출 | 동일 | CLI 구현 완료, BE 7.9 대기 |
| v3 | [BE_연동_필드표_v3.md](BE_연동_필드표_v3.md) | + 취향 문장·리뷰 | + `giftPreference` · `reviews[≤10]` | 동일 | 동일 (태그는 AI 보관) | 7.6 스키마만 준비, 모델 단계 미구현 |

버전 공통 규칙(`profileStatus` 생애주기 · 재시도 정책 · 경합 · 오류 코드)은 **v1 문서 §3·§6**에 있고, v2·v3는 바뀌는 것만 적고 v1을 가리킨다.

그림: `assets/be-seq/v1|v2|v3/` — `assets/build_be_sequences.py`가 세 문서의 mermaid 블록에서 만든다.
