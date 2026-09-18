# 선잘알 AI 백엔드 서비스 (`KTB4-12th-AI`)

선물 추천 및 대화 검색을 제공하는 AI 전용 백엔드 서비스입니다.

---

## 1. 독립 기능별 작업 공간 (Architecture)

팀원 간의 Git 충돌을 방지하고 각자의 모듈을 독립적으로 개발/테스트할 수 있도록 기능별로 작업 공간을 분리했습니다.

```text
KTB4-12th-AI/
├── search_catalog/           # 🔍 [질문자님 전용] 상품 검색 & 카탈로그 색인 모듈
│   ├── search/               # 검색 코어 엔진 (SearchService, DTO)
│   ├── catalog/              # 상품 export 수신 & pgvector 스냅샷 색인
│   ├── console/              # 🖥️ 검색기 단독 테스트용 웹 콘솔 (http://localhost:8501)
│   └── tests/                # 검색 전용 단위 테스트
│
├── profiler/                 # 👤 [동료 전용] 수신자 취향/리뷰 분석 프로파일러 모듈
│   ├── service.py            # ProfileService (SearchService 주입 연동)
│   └── README.md             # 동료를 위한 연동 가이드
│
├── server/                   # 🚀 [통합 배포용] 프로덕션 FastAPI 서버 엔트리포인트
│   ├── main.py
│   └── config.py
│
├── docker-compose.yml        # 로컬 개발용 (AI 앱 + PostgreSQL pgvector:pg16)
├── Dockerfile
└── requirements.txt
```

---

## 2. 모듈별 독립 실행 방법

### (1) 로컬 개발 환경 세팅
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### (2) 검색기 단독 웹 콘솔 실행 (질문자님 튜닝용)
다른 서버나 복잡한 설정 없이, 검색 엔진과 콘솔 UI만 즉시 띄워서 테스트할 수 있습니다.
```bash
python -m search_catalog.console.app
```
* 브라우저에서 `http://localhost:8501` 접속

### (3) 전체 통합 서버 실행 (프로덕션 모드)
```bash
uvicorn server.main:app --reload --port 8000
```
* Swagger Docs: `http://localhost:8000/docs`
* Health Check: `http://localhost:8000/health`

### (4) 로컬 Docker Compose 실행 (pgvector 포함)
```bash
docker compose up -d
```

---

## 3. 테스트 실행
```bash
pytest search_catalog/tests/
```
