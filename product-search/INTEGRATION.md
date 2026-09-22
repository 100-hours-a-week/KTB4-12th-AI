# 프로파일러·챗봇 연결 메모

2026-09-22에 `main@733ce6e`와 `feat/emet-profiler-init@3daedc3`의 계약을 확인했다. 이 브랜치는 검색 구현을 독립 폴더에 추가한 것이다. 프로파일러 코드에 직접 수정하거나 DB 변경을 적용하지 않았다.

## 지금 로컬에서 같이 띄우기

첫 공동 테스트는 각각의 가상환경/프로세스를 유지하고 `http://127.0.0.1:4325`를 호출하는 방식이 가장 적은 변경으로 가능하다. 프로파일러는 Python >=3.12, 검색기는 >=3.13이다. 별도 프로세스 실행에는 가상환경을 합칠 필요가 없다. 같은 호스트이면 loopback, 별도 컨테이너이면 팀 Compose 서비스 이름/내부 포트로 연결해야 한다. Compose 설정은 아직 없다.

`POST /api/search`로 필터·순위를 테스트하고 `POST /api/products`로 source ID 원문 조회를 테스트한다. 이 HTTP 경로는 QA 기록을 저장하며 요청 source=qa다. 운영 프로파일러 전용 API라는 뜻은 아니다.

서버를 띄운 뒤 바로 실행할 수 있는 예시는 [examples/search_client.py](examples/search_client.py)다. `product-search/`에서 `uv run python examples/search_client.py`를 실행한다. `--base-url`, `--query`, `--max-price`를 바꿀 수 있다.

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:4325", timeout=30) as client:
    response = client.post("/api/search", json={
        "query": "휴대하기 좋은 스피커",
        "filters": {"minPrice": 30000, "maxPrice": 100000},
        "limit": 10,
    })
    response.raise_for_status()
    result = response.json()
    source_ids = [hit["id"] for hit in result["hits"]]
    response = client.post("/api/products", json={
        "ids": source_ids, "snapshotId": result["snapshotId"],
    }) if source_ids else None
    if response is not None:
        response.raise_for_status()
        details = response.json()
```

검색 응답에서 `hits[].id`는 조회에 다시 넣을 source ID, `hits[].backendProductId`는 아직 null이다. `hits[].categoryId`는 문자열 소분류 ID, `price`는 원화 정수, `availability`는 3상태 값이다. `image`, `imageLarge`, `imageFallback`은 검색 서버 기준 상대 URL이다. 별도 프론트엔드에서 이미지를 표시할 때 해당 검색 서버의 접근 가능한 base URL과 연결한다.

상태 처리는 다음과 같다. 응답 필드 정의와 요청 검증 모델은 서버 `/docs`에서 볼 수 있다.

| 응답 | 호출 측 처리 |
|---|---|
| 200 + `status: NO_MATCH`, `hits: []` | 결과 없음. 오류로 바꾸지 않고 조건 재조정 |
| 200 상품 조회 + `missingIds` | 없는 ID를 별도 처리. 다른 상품 ID로 임의 치환하지 않음 |
| 422 | 요청 필드/조건 수정. 자동 반복 호출하지 않음 |
| 409 상품 조회 | 스냅샷 변경. 새 검색부터 다시 수행 |
| 503 | 실행기/부하 오류. 제한된 지연 재시도 또는 호출 실패 전달 |

`snapshotId`는 HTTP 검색 요청 필드가 아니라 검색 응답 및 상품 조회 요청 필드다. Python 코어 호출에서는 `snapshot=` 인자로 여러 검색을 같은 버전에 묶는다. HTTP로 여러 검색을 묶는 쪽은 각 응답 snapshotId가 같은지 확인해야 한다. 하나의 스냅샷으로 시작한 작업에 서로 다른 버전의 결과를 섞지 않는다.

Python 앱 하나로 합칠 때는 같은 3.13 환경에서 아래 공용 인스턴스를 앱 시작 시 한 번 생성하고 두 서비스에 전달한다. 요청마다 `open_search`를 만들지 않는다. 코어는 FastAPI 라우트에 의존하지 않으며 프로파일·대화 DB를 소유하지 않는다. 현재 `search` 패키지를 import하려면 `product-search/`가 Python 모듈 경로에 있어야 한다. 루트 경로도 인자로 전달하므로 현재 작업 폴더에 의존할 필요는 없다.

```python
from pathlib import Path
from search import open_search, SearchRequest, Filters

async def example(search_root: Path):
    async with open_search(search_root) as search:
        snapshot_id = search.snapshot_id
        result = await search.search(
            SearchRequest(query="커피 도구", filters=Filters(max_price=50000)),
            snapshot=snapshot_id,
            source="profile",  # 챗봇은 chat
        )
        return search.get_products(
            [hit["id"] for hit in result["hits"]], snapshot=snapshot_id
        )
```

## 자동으로 호환되지 않는 부분

| 프로파일러 쪽 현재 계약 | 검색기 쪽 현재 계약 | 연동 시 처리 |
|---|---|---|
| `CatalogReader.active()`가 전체 상품과 버전을 반환 | 검색 후보/상품 조회, 문자열 snapshot ID | 전체 CatalogReader를 SearchService로 바로 바꿀 수 없음. 조회/검색 adapter 역할을 나눔 |
| `by_id(product_id: int)` | `get_products(ids: list[str])` | Backend productId ↔ sourceProductId 매핑 필요 |
| 숫자 `categoryId` | `CAT-…` 문자열 소분류 ID | Backend가 회신한 분류 ID 매핑 사용 |
| `available: bool` | available / unavailable / unknown | 현재 전부 unknown. 프로파일러에서 미확인 상태 정책/타입을 명시해야 함 |
| 고정 파일 UUID 버전 | 카탈로그 내용 해시 기반 snapshot ID | 같은 원본 버전이라는 별도 매핑을 기록. UUID와 문자열 해시를 같은 값으로 가정하지 않음 |
| `updatedAt` | 수집 시각이 일부 미확인인 고정 스냅샷 | 실행/다운로드 시각을 상품 갱신 시각으로 바꾸지 않음 |

현재 profiler `FileCatalogReader`의 raw 변환은 `product_id`에서 숫자를 떼고, CAT 번호를 산술로 바꾸며, 과거 ON_SALE/sold_out로 available을 만든다. 이 값은 Backend가 발급한 PK/현재 판매 상태라는 근거가 없다. 또한 이번 Backend 간편 전달본 `source/products.json`에는 raw adapter가 요구하는 `category` 이름 필드가 없으므로 그대로 넣으면 해당 경로에서 읽지 못한다.

따라서 첫 테스트는 **검색 API가 동작하는지**와 **실제 Backend ID로 end-to-end 조인이 되는지**를 나눠 진행한다. source ID를 숫자로 잘라 넘기는 임시 변환을 운영 매핑으로 사용하지 않는다. 필요한 매핑 양식은 기존 Backend 데이터 전달 패키지에 있다.

현재 PostgreSQL `ai_search.products`에는 상품 정보 6필드만 적재하는 코드가 main에 있다. 이 검색 구현은 그 테이블을 읽지 않는다. DB 변경을 검색 스냅샷으로 반영하는 export/재인덱싱 경로가 후속 연동 작업이다.

## 상대 브랜치에서 코드 병합하기

```sh
git fetch origin
# 현재 작업을 먼저 커밋/보관한 뒤, 본인 브랜치에서 실행
git merge origin/feat/dylan-product-search
```

이 브랜치에서 상대 프로파일러를 자동으로 병합하거나 main에 반영하지 않았다. 병합한 뒤 별도 데이터 자산을 받고 README의 실행 검증을 진행한다. API 연동 후에는 ID 불일치, unknown 판매 상태, 0건 결과, 버전 불일치, 503 처리까지 함께 확인한다.
