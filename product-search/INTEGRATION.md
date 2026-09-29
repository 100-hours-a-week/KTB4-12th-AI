# 검색 API 호출

검색 서버 주소를 지정해 HTTP로 호출한다. 요청·응답 스키마는 실행 중인 서버의 `/docs`, 필터 설명과 실행 예제는 `/guide`에 있다.

```sh
curl http://127.0.0.1:4326/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"텀블러","limit":5}'
```

| 경로 | 용도 |
|---|---|
| `GET /readyz` | 검색 준비 상태 |
| `GET /v1/metadata` | 사용 가능한 분류·브랜드·데이터 시각 |
| `POST /v1/search` | 검색 후보 조회 |
| `POST /v1/products` | 상품 ID 목록으로 상세 조회 |

Python 호출 예제는 [examples/search_client.py](examples/search_client.py)에 있다. [search_client.py](search_client.py)는 `httpx`를 사용하며 호출 앱에서 재사용하고 종료 시 닫는다.

- `productId`는 Backend 상품 번호다. 원본 ID의 숫자를 추출해 대체하지 않는다. 큰 정수는 JavaScript Number 변환 시 정밀도에 주의한다.
- 페이지·상세 조회에는 첫 응답의 `snapshotId`를 전달한다. 409이면 새 검색부터 시작한다.
- `NO_MATCH`는 후보 없음이다. 503 등 실행 오류를 후보 없음으로 취급하지 않는다.
- 판매 상태 기본값은 `any`다. 조건은 호출자가 선택하며 export 시점 이후의 가격·재고는 보장하지 않는다.
- `/v1/search`는 검색 기록을 저장하지 않는다. QA 웹의 `/api/search`는 기록을 저장한다.
- `X-Search-Source`의 `chat`·`profile`은 현재 처리 우선순위 구분이며 인증 수단이 아니다. 기본값은 `profile`이다.

Backend export 계약은 [AI 위키 §7.9](https://github.com/100-hours-a-week/KTB4-12th-wiki/wiki/모델-API-설계#79-상품-전체-export)를 따른다. 데이터 준비 명령은 [README](README.md)에 있다.

## 인증과 준비 상태

HF 데모처럼 Bearer 인증이 필요한 서버는 `SearchClient(base_url, token=os.environ["DEMO_TOKEN"])`으로 호출한다. 토큰을 생략하면 기존 로컬 호출과 같다. 토큰은 URL이나 로그에 넣지 않는다.

클라이언트를 다른 앱에 복사할 때는 `search_client.py`와 `http_security.py`를 함께 둔다. 인증 호출은 loopback 외에는 HTTPS를 사용하고 리다이렉트를 따르지 않는다. 신뢰하는 사설망에서 HTTP를 써야 할 때만 `allow_insecure_http=True`를 명시한다.

`/api/`·`/v1/` 요청 본문은 UTF-8 기준 64 KiB까지 받으며, 초과하면 JSON 파싱·검색·QA 저장 전에 `413 REQUEST_TOO_LARGE`를 반환한다. 이 제한은 `Content-Length`가 없는 스트리밍 요청에도 적용된다.

`/healthz`는 프로세스 생존 여부(`alive`), `/readyz`는 실제 검색 준비 여부다. 준비 검사는 30초마다 여유 시 캐시를 우회해 수행하며 임베딩 프로세스 종료와 처리 태스크 중단도 반영한다. 실패 시 `/readyz`는 503, 복구 후 200이다. 배포 검증은 `/readyz?probe=true`로 새 검사를 강제한다. 이 경로는 서비스 내부 또는 인증 뒤에서 사용한다.

QA 제보는 선택적 `submissionId`(32자리 소문자 hex)를 받는다. 재시도에는 같은 ID와 내용을 보내고, 다른 내용은 새 ID를 사용한다. 같은 ID에 다른 내용은 409다. 외부 보관이 설정된 경우 보관 완료 후에만 `saved: true`를 반환하고, 실패는 503 `FEEDBACK_STORAGE_UNAVAILABLE`이다. 검색 서비스 API의 무기록 정책은 동일하다.
