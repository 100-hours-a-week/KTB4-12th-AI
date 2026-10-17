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
