# 상품 검색 · 로컬 연동/QA용

프로파일러·챗봇이 사용할 검색 코어와 사람용 QA 화면이다. `product-search/` 안에서 독립 실행하며 기존 AI 래퍼와 `workbench/dylan/` 적재 코드를 수정하지 않는다.

**구현된 범위:** BM25F + dense + RRF 검색, 공통 조건 필터, 상품 ID 조회, FastAPI, 사진·설명·가격을 보는 QA 화면, 검색 기록·관련성 피드백, 압축 이미지와 브라우저 캐시.

**아직 연결하지 않은 범위:** PostgreSQL 실시간 검색/동기화, Backend 숫자 ID 매핑, 기존 프로파일러·챗봇에 adapter 주입, 인증·운영 배포. 현재 검색 대상은 DB가 아니라 전달받은 상품 스냅샷을 메모리에 올린 인덱스다. Git 병합 성공과 서비스 연동 완료는 별개다.

## 먼저 코드만 확인하기

Python 3.13 이상과 uv를 사용한다. 검색 경계 테스트에는 실제 상품 데이터·Jina 모델·Node가 필요 없다. Kiwi와 그 모델 패키지는 아래 uv 명령으로 함께 설치한다.

```sh
cd product-search
uv sync --frozen --dev
uv run pytest -q
```

| 파일 | 역할 |
|---|---|
| `search/models.py` | 검색어·필터·선호·상품 조회 요청 계약 |
| `search/tokenization.py` | Kiwi 문서/질의 토큰화, 품사 필터, 모델명 보존 |
| `search/service.py` | 필터, BM25F, dense cosine, RRF, 결과·상품 상세 |
| `search/embedding.py` | Node 실행기, 대기열, 질의 캐시·중복 요청 공유 |
| `search/bootstrap.py` | 모델·카탈로그 시작/종료와 공용 인스턴스 생성 |
| `app.py` | HTTP API, QA 기록·피드백, 정적 파일·이미지 응답 |
| `static/` | 빌드 도구 없이 실행하는 QA 화면 |
| `embedding/` | 고정 모델 다운로드 정보, Node 추론·인덱스 빌드 |
| `tools/` | 별도 자산 설치, 모델 다운로드, 원본에서 재생성 |
| [INTEGRATION.md](INTEGRATION.md) | 프로파일러/챗봇 연결 방식과 현재 계약 차이 |

## 실제 상품으로 실행하기

공개 Git에는 실제 상품·이미지·벡터·모델·실행 기록을 넣지 않았다. Dylan에게 팀 내부 전달 파일 **2개**를 받아 한 폴더에 둔다. Backend 적재용 `…-data.zip`과 아래 **검색 실행용 runtime.zip은 용도가 다르다.**

- `product-search-runtime-20260922-v1.zip`: 카탈로그·기존 벡터·manifest, 약 12.54 MB. 버전/해시는 [runtime-bundle.json](runtime-bundle.json).
- `product-catalog-20260922-v1-images.zip`: 기존 Backend 전달본과 같은 압축 이미지, 약 203.60 MB.

다음 명령은 `product-search/`에서 실행한다. `SEARCH_ASSETS_DIR`은 받은 ZIP 두 개가 있는 폴더로 바꾼다. 아래 도구는 ZIP 전체 해시를 검증한 뒤 설치한다. 실행 중인 검색 서버는 자산 교체 전에 종료한다.

```sh
uv sync --frozen --dev
npm ci --prefix embedding --ignore-scripts --no-audit --no-fund
uv run python tools/download_model.py

export SEARCH_ASSETS_DIR="$HOME/Downloads"
uv run python tools/install_assets.py \
  --runtime "$SEARCH_ASSETS_DIR/product-search-runtime-20260922-v1.zip" \
  --images "$SEARCH_ASSETS_DIR/product-catalog-20260922-v1-images.zip"

./start.sh
```

Node.js도 설치되어 있어야 한다. macOS/Linux용 로컬 실행 경로이며 Windows 자체 실행은 검증하지 않았다(WSL/Linux 사용 가능 여부는 해당 환경에서 확인). 고정 Node 의존성은 `embedding/package-lock.json`에 있다.

주소는 `http://127.0.0.1:4325`, Swagger는 `/docs`, 상태 확인은 `/healthz`다. 포트 변경은 `SEARCH_PORT=4336 ./start.sh`. 모델 초기화가 끝난 뒤 요청을 받는다. 모델 4파일은 게시자 Hugging Face의 고정 revision에서 받아 SHA-256을 검사한다. 이후 검색은 로컬 추론이며 외부 임베딩 API 키가 필요 없다.

```sh
curl -sS http://127.0.0.1:4325/healthz
curl -sS http://127.0.0.1:4325/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"텀블러","filters":{"maxPrice":30000},"mode":"hybrid","limit":5}'
```

팀 내부·교육 시연을 전제로 한 로컬 QA 코드다. 인증 기능은 없으며 현재 `start.sh`는 loopback에만 바인딩한다. 외부 공동 QA는 팀 접근 제한을 갖춘 별도 접속 경로가 필요하다. 실제 사용자 리뷰·개인정보를 QA 검색어나 피드백에 넣지 않는다.

## API와 검색 의미

| API | 용도 |
|---|---|
| `GET /api/metadata` | 분류·브랜드·상품 유형과 스냅샷 정보 |
| `POST /api/search` | 검색. `query`, `filters`, `preferences`, `mode`, `limit`, `offset` |
| `POST /api/products` | `ids`와 선택적 `snapshotId`로 상품 원문·속성 조회 |
| `GET /api/searches/{searchId}` | 저장한 QA 실행 조회. UI에서는 `/?run=...` |
| `POST /api/feedback` | QA 검색/상품에 관련성·조건 위반·누락 의견 기록 |

필터: `minPrice`, `maxPrice`, `categoryIds`, `excludeCategoryIds`, `brands`, `excludeBrands`, `productTypes`, `excludeProductIds`, `availability`. 같은 목록은 OR, 다른 필드는 AND이며 제외가 우선이다. 가격 경계를 포함한다. 카테고리 필터는 `CAT-…` 소분류 ID이며 대분류는 해당 소분류 목록으로 변환한다. 브랜드는 NFKC·대소문자·공백 정규화 후 정확 일치다. 미지원 필드·알 수 없는 분류/브랜드·잘못된 가격은 422다.

모드는 `hybrid`, `lexical`, `dense`. 빈 질의는 임베딩 없이 분류가 고르게 섞인 목록을 제공한다. limit 1~100, offset 0~5000. `eligibleCount`는 조건에 맞는 전체 건수, `candidateCount`는 두 검색에서 모은 후보 수다. 일반 검색은 검색 방식별 상위 100개를 합쳐 후보 최대 200개이므로 무제한 전체 검색결과 페이징이 아니다.

키워드 토큰은 Kiwi 0.23.2 / 모델 0.23.0의 CoNg로 분석한다. NFKC·casefold 후 명사·동사·형용사·어근·부사·영문·숫자 형태소를 남기고 조사·어미는 제외한다. 영문·숫자 모델명 원형도 보존한다. 문서와 질의는 같은 정책을 쓰고, 임의의 두 글자 조각은 만들지 않는다. 인덱싱은 중복 필드를 묶어 Kiwi worker 2개로 일괄 분석하며 질의 토큰 LRU는 4,096개다. API metadata의 `lexicalTokenizer`에서 적용 버전을 확인할 수 있다. [Kiwi 공식 문서](https://github.com/bab2min/kiwipiepy)

BM25F는 상품명 4, 브랜드 3, 분류·종류 2, 설명·속성 1, 가공 태그 0.5의 초기 가중치다. dense는 이름·브랜드·분류·종류·속성·설명을 사용하며 가공 태그는 넣지 않는다. 768차원 float32 벡터를 정규화해 exact cosine으로 검색한다. RRF 상수 30, 선호 분류 ×1.10, 비선호 분류 ×0.75는 아직 평가로 최적화하지 않은 초기 설정이다. 비선호는 제외가 아니며 점수는 추천 확률이 아니다.

원본 상품 ID는 `KAKAO_GIFT:…`, `backendProductId`는 null이다. 가격은 저장 당시 값, 현재 판매 상태는 모두 unknown이다. 따라서 이 데이터에서 `availability=available` 필터는 결과가 없다. unknown을 true/false로 추정하지 않는다.

## 운영 방식과 재생성

앱 worker 1개가 공용 Node 추론기 1개와 검색 스레드 최대 2개를 사용한다. 모델 CPU 스레드 2개, 질의 대기열 24개, 질의 LRU 2,048개, 결과 LRU 256개다. `chat → qa → profile` 우선순위는 Python 코어 호출의 신규 대기 작업에 적용하며 실행 중 호출을 선점하지 않는다. HTTP 검색은 QA adapter이므로 source=qa로 기록한다. 한계 초과·추론 오류는 503이며 키워드 검색으로 조용히 대체하지 않는다.

UI 첫 화면은 384 AVIF를 바로 요청하고 아래 이미지는 lazy loading한다. 상세는 768 AVIF, 대체 이미지는 384 WebP다. 내용 해시 이미지/CSS/JS는 1년 immutable 캐시, HTML은 재검증, API는 no-store다. 서버/브라우저 검색 결과 캐시와 요청 순서 처리를 통해 재검색 중 화면 교체를 줄인다. QA 기록은 로컬 `runtime/qa.sqlite3`, 최근 검색 5,000개를 유지한다.

별도의 원본 `gift-catalog-20260915-v1` 전체 패키지가 있을 때 재생성할 수 있다. 아래 원본은 Backend 간편 전달본과 형식이 다르며 `catalog_enriched.json`, `category_taxonomy`, 원본 `image_path` 파일이 필요하다.

```sh
uv run python tools/prepare_catalog.py --source /path/to/gift-catalog-20260915-v1
uv run python tools/build_images.py
cd embedding
node build.mjs
```

모델은 [Jina nano retrieval](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano-retrieval)의 ONNX q4f16이며 라이선스는 CC BY-NC 4.0이다. 입력은 상품 `Document: `, 질의 `Query: `, 최대 1,024토큰이다. 전체 상품 원문은 보존하고 잘림 정보를 결과에 표시한다. 상품·이미지 이용 권한과 모델 이용 조건은 별도로 확인해야 한다.

재생성/자산 설치는 가동 중 서버를 갱신하지 않는다. 반영하려면 재시작한다. 현재 스냅샷 ID 일치 검사만 제공하며 과거 인덱스의 온라인 버전 전환은 구현하지 않았다. 검증한 범위는 [VERIFICATION.md](VERIFICATION.md)에 기록한다.
