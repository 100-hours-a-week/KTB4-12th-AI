# 선잘알 AI 백엔드 서비스 (`KTB4-12th-AI`)

선물 추천 및 대화 검색을 제공하는 AI 전용 백엔드 서비스입니다.

---

## 1. 프로젝트 아키텍처 구조

배포용 **공통 서비스 스켈레톤(`app/`)**과 개발자별 **개인 작업 공간(`workbench/`)**으로 구성되어 있습니다.

```text
KTB4-12th-AI/
├── app/                          # 🏛️ [프로덕션 공통 서비스 스켈레톤] (Docker 배포 대상)
│   ├── main.py                   # FastAPI 메인 엔트리포인트
│   ├── config.py                 # 환경변수 설정 (Pydantic Settings)
│   ├── search/                   # 🔍 [Dylan] 상품 검색 코어 엔진 (SearchService, DTO)
│   ├── catalog/                  # 📦 [Dylan] 상품 export 수신 & pgvector 스냅샷 색인
│   ├── profile/                  # 👤 [Emet] 수신자 취향/리뷰 분석 프로파일러 코어
│   └── chat/                     # 💬 대화 상태 관리 및 SSE 스트리밍
│
├── workbench/                    # 🧪 [개발자별 개인 작업실] (실험, 콘솔, 튜닝 스크립트)
│   ├── dylan/                    # 👈 Dylan 전용 작업 공간
│   └── emet/                     # 👈 Emet 전용 작업 공간
│
├── tests/                        # 공통 단위/통합 테스트
├── docker-compose.yml            # 로컬 개발용 (AI 앱 + PostgreSQL pgvector:pg16)
├── Dockerfile                    # 배포용 컨테이너 빌드 파일 (app/만 패키징)
└── requirements.txt
```

---

## 2. 모듈 실행 가이드

### (1) 로컬 개발 환경 세팅
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### (2) 전체 FastAPI 서비스 실행
```bash
uvicorn app.main:app --reload --port 8000
```
* Swagger Docs: `http://localhost:8000/docs`
* Health Check: `http://localhost:8000/health`

### (3) 로컬 Docker Compose 실행 (pgvector 포함)
```bash
docker compose up -d
```

### (4) 단위 테스트 실행
```bash
pytest tests/
```
