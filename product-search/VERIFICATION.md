# 브랜치 공유 전 확인 · 2026-09-22

최신 `origin/main@733ce6e`에서 별도 체크아웃을 만들고 `product-search/`에 코드만 옮긴 뒤 검사했다. 원래 로컬 검색기·AI 래퍼·DB 적재 코드는 변경하지 않았다.

검증 환경: macOS arm64, Python 3.13.14, Node v26.7.0. 다른 OS/Node 버전과 프로파일러 통합 가상환경은 아직 검증하지 않았다.

| 확인 | 결과 |
|---|---|
| `uv sync --frozen --dev` | 새 가상환경 설치 성공 |
| `npm ci --ignore-scripts --no-audit --no-fund` | 고정 Node 의존성 설치 성공 |
| `tools/download_model.py` | 고정 HF revision의 모델 4파일을 실제 다운로드하고 각 해시 일치 |
| `tools/install_assets.py` | 팀 내부 ZIP 두 개의 해시 확인 후 상품 4,231개·이미지 12,615개 설치 |
| `uv run pytest -q` | 기존 검색 경계 테스트 12개 통과 |
| 별도 포트 서버 시작 | healthz ready, 상품 4,231개·소분류 57개 |
| `examples/search_client.py` | HTTP 검색 5건 → 같은 snapshot의 상세 5건 조회, missingIds 없음 |
| hybrid / lexical / dense | 세 모드에서 가격 상한 조건 준수 |
| 상품 조회 | 존재하는 상품 조회와 missingIds 분리 |
| 잘못된 snapshot | 상품 조회 409 |
| 잘못된 가격 범위 | 검색 422 |
| availability=available | 전부 unknown인 현재 자료에서 0건 |
| QA 검색 기록 조회 | 저장한 결과 재조회 일치 |
| HTML·정적 자산 | HTTP 200, 내용 버전 치환 |
| AVIF 384/768·WebP 384 | 세 형식 HTTP 200, immutable 캐시 헤더 |
| API 캐시 | no-store |

이번 검증은 새 체크아웃의 실행 경로와 통신 계약 확인이다. UI 코드는 기존 로컬 구현 그대로 옮겼으며 이번 브랜치에서는 별도 브라우저 시각 검사를 반복하지 않았다. 운영 성능/부하 측정, 정답 기반 관련성 평가, 프로파일러 end-to-end 조인, 실제 Backend DB ID·판매 상태·갱신 동기화는 완료 범위가 아니다.
