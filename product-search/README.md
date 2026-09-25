# 상품 검색

FastAPI 검색 API와 확인용 웹이다. Kiwi·BM25F 키워드 검색과 로컬 임베딩 검색을 함께 사용한다.

## Docker로 실행

Docker Engine과 Compose가 필요하다. 명령은 `product-search/`에서 실행한다.

```sh
docker compose build search
docker compose run --rm sample
docker compose up -d --wait search
```

- 웹: http://127.0.0.1:4326
- API 예제: http://127.0.0.1:4326/guide
- 스키마: http://127.0.0.1:4326/docs
- 준비 확인: http://127.0.0.1:4326/readyz

`sample`은 합성 상품 3개로 검색 자료를 만든다. 기존 데이터가 있으면 덮어쓰지 않는다. 개인 Dataset이나 토큰은 필요 없다. 최초 빌드는 공개 모델을 다운로드하며 revision과 해시를 검증한다. 모델 라이선스는 [model.lock.json](embedding/model.lock.json)의 CC-BY-NC-4.0이다.

데이터와 검색 기록은 각각 `data`, `qa` Docker 볼륨에 저장한다. `docker compose down`은 보존하고 `down -v`는 삭제한다. 호스트에는 localhost로만 공개하며 자체 인증은 없다.

이미 제공받은 이미지를 쓰려면 `.env.example`을 `.env`로 복사해 `AI_SEARCH_IMAGE`를 해당 주소로 지정하고 `docker compose pull search`를 실행한다. 이후 위의 `sample`, `up` 명령을 사용한다. 설정 예시는 [.env.example](.env.example)을 참고한다.

## Backend export로 준비·갱신

합성 데이터 설치 없이도 실행할 수 있다. Backend 주소와 서비스 토큰을 환경변수로 준비한다. Docker 컨테이너에서 접근 가능한 주소를 사용한다.

```sh
docker compose run --rm sync \
  --export-url "$BACKEND_EXPORT_URL" \
  --categories-url "$BACKEND_CATEGORIES_URL"
```

기본은 입력 검증과 변경량 확인이다. 같은 명령에 `--apply`를 붙이면 임베딩·검색 인덱스 검증 후 활성 데이터를 교체한다. 최초 생성이 끝나면 `docker compose up -d --wait search`로 실행한다. 갱신 실패 시 기존 데이터를 유지한다. 자동 스케줄러는 없다.

- export 계약: [AI 위키 §7.9](https://github.com/100-hours-a-week/KTB4-12th-wiki/wiki/모델-API-설계#79-상품-전체-export)
- `BACKEND_SERVICE_TOKEN`은 Git에서 제외되는 로컬 `.env` 또는 셸 환경변수로 전달한다.
- 카테고리는 현재 Backend `develop`의 `/products/categories` 계층 응답을 사용한다. 이 응답은 위키의 평면·페이지네이션 예시와 다르다. 실제 Backend 접속은 아직 검증하지 않았다.
- export에 없는 이미지·상품 유형·원본 ID는 새로 만들지 않는다. 처음 export만으로 준비하면 이미지 없이 표시되며 해당 값은 비어 있거나 null이다.

## 직접 준비한 파일 사용

[products-export.json](examples/products-export.json)과 [categories.json](examples/categories.json)은 실행용 합성 예제다. 같은 응답 형식으로 준비한 파일을 읽을 수도 있다.

```sh
docker compose run --rm -v "$PWD/my-input:/input:ro" sync \
  --export-file /input/products.json --categories-file /input/categories.json --apply
```

이는 Backend 계약을 새로 정의하지 않으며, 입력 형식은 위키와 현재 파서 검증을 따른다.

## 개발·테스트

Python 3.13, uv, Node.js 24를 사용한다.

```sh
uv sync --frozen --python 3.13
uv run pytest -q
node --test tests/test_ui.mjs
```

테스트는 합성 자료를 사용하며 실제 상품·모델 없이 실행된다. Node.js가 없으면 추론기 프로세스 테스트가 건너뛰어지므로 위 환경에서 확인한다.

Docker 없이 실행하려면 다음을 추가로 실행한다.

```sh
npm ci --prefix embedding --ignore-scripts --no-audit --no-fund
uv run python tools/download_model.py
uv run python tools/sync_catalog.py \
  --export-file examples/products-export.json --categories-file examples/categories.json --initial-only --apply
./start.sh
```

로컬 실행 포트는 4325다. API 사용은 [연동 안내](INTEGRATION.md)를 참고한다.
