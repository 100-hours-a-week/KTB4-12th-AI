"""설정 — 환경변수 → 객체 하나. 실험 config.json의 값 중 서비스에 필요한 것만 옮긴다.

읽는 순서(pydantic-settings): 환경변수 > .env 파일 > 아래 기본값. 접두사 PROFILING_ 을 붙인 이름만 읽는다
(예: PROFILING_BACKEND_BASE_URL). 팀원 골격은 아직 없다(09-18 초기 골격은 되돌려짐) — 합칠 때 접두사·이름을 맞추더라도
여기 한 파일만 바꾼다. 코드는 settings.BACKEND_BASE_URL 처럼 속성으로만 쓴다.

값의 출처
  - POOL_SIZE 30                  : 모델 API 설계 §7.7 (7.6 의 비선호 5·리뷰 10 상한은 schemas.py 의 max_length — 계약값, 설정 아님)
  - CALLBACK_TIMEOUT_S 5          : 실험 하네스 config.json timeout_s(120)은 LLM용. 콜백은 짧게
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 추출기별 기본 모델 — TAGGER_MODEL 을 비우면 이것을 쓴다. 추출기와 맞지 않는 모델 이름이 넘어가 요청마다 태그가 실패하는 것을 막는다
# (예: openrouter 에 Ollama 모델 이름을 보내면 http_400 — 추천은 나가므로 겉으로는 정상처럼 보인다)
DEFAULT_TAGGER_MODEL: dict[str, str] = {"openrouter": "google/gemini-3.5-flash-lite", "ollama": "gemma4:12b"}


class Settings(BaseSettings):
    """서비스 설정. 대문자 필드명 = 환경변수 이름에서 접두사를 뗀 것 (PROFILING_POOL_SIZE → POOL_SIZE)."""

    model_config = SettingsConfigDict(
        env_prefix="PROFILING_",
        env_file=".env",            # 실행 위치(profiling/) 기준. 없어도 된다
        env_file_encoding="utf-8",
        extra="ignore",             # .env에 다른 서비스 변수가 섞여 있어도 무시
    )

    # ---- 바깥 연결
    BACKEND_BASE_URL: str = Field(default="http://localhost:8081", description="7.7 콜백·7.9 export 대상. 로컬 기본값은 :8081 — 서버는 Backend 내부 주소")
    SERVICE_TOKEN: str = Field(default="", description="Backend↔AI 서비스 토큰(Bearer). 비어 있으면 7.6 토큰 검사 생략(로컬 전용)")
    CALLBACK_TIMEOUT_S: float = Field(default=5.0, gt=0, description="7.7 POST 한 번의 타임아웃(초)")
    CALLBACK_MAX_ATTEMPTS: int = Field(
        default=3, ge=1,
        description="7.7 이 5xx·네트워크로 실패하면 같은 슬롯 안에서 다시 보내는 최대 시도 수(최초 포함). 다 써도 실패면 "
                    "RESULT_READY 로 남겨 Backend 가 같은 번호로 다시 요청할 때 재전송한다. 200·409·4xx 는 재시도하지 않는다")
    CALLBACK_BACKOFF_S: float = Field(
        default=0.5, gt=0,
        description="재시도 대기의 첫 값(초). 다음은 4배씩 — 기본 0.5초·2초. 대기는 슬롯을 잡은 채 하므로 "
                    "최악은 0.5 + 2 + 타임아웃 5초 × 3 ≈ 17.5초")

    # ---- DB (로컬 compose 기본값. 합칠 때 접두사만 맞춤)
    DATABASE_URL: str = Field(default="",                # 기본값 없음(10-15) — .env 또는 환경변수로. 비면 기동 · 마이그레이션이 멈춘다
                              description="SQLAlchemy URL (psycopg 3). Alembic env.py도 이 값을 쓴다. 앱은 운영 · 개발 서버에서 "
                                          "앱 전용 계정(ai_profile 읽기·쓰기, ai_catalog 읽기만, 이름은 PROFILING_APP_DB_ROLE)으로, 마이그레이션 · 적재는 소유자 계정으로")
    DB_REFUSE_SUPERUSER: bool = Field(
        default=False,
        description="true 면 앱이 슈퍼유저 계정으로 접속할 때 기동을 거부한다(운영 · 개발 서버 · compose 는 true + 앱 전용 계정). "
                    "false(로컬 uv 기본)면 경고 로그만")
    # 커넥션 풀(10-13 DB 검토) — 슬롯마다 잠금 커넥션 1개를 분석 내내(LLM 대기 · 콜백 포함) 쥐고, 접수 폴링 · /health 가 IO_THREADS 만큼 더 쓴다
    DB_POOL_SIZE: int | None = Field(
        default=None, ge=1,
        description="상시 커넥션 수. 비우면 PROFILING_SLOTS + IO_THREADS. pool_size + max_overflow 가 그보다 작으면 기동 거부 "
                    "(슬롯이 커넥션을 기다리다 DB_POOL_TIMEOUT_S 뒤 실패하는 상태를 만들지 않게)")
    DB_MAX_OVERFLOW: int = Field(default=2, ge=0, description="상시 수를 넘어 잠깐 더 여는 커넥션 수(기동 때 카탈로그 · 임베딩 적재 등)")
    DB_POOL_TIMEOUT_S: float = Field(default=10.0, gt=0, description="풀이 다 찼을 때 커넥션을 기다리는 상한(초). 넘으면 그 작업만 실패")
    # 시간 상한(10-13 DB 검토) — 접속 옵션으로 세션마다 건다. 0 = 무제한. 넘으면 그 문장만 취소되고(예외) 파이프라인이 FAILED 로 기록
    DB_CONNECT_TIMEOUT_S: int = Field(default=5, ge=1, description="DB 접속 상한(초)")
    DB_STATEMENT_TIMEOUT_MS: int = Field(
        default=5000, ge=0,
        description="문장 하나의 상한(ms). 앱 문장은 전부 수십 ms 안쪽(가장 긴 것은 카탈로그 전체 읽기) — 멈춘 문장이 슬롯을 영영 잡지 않게")
    DB_LOCK_TIMEOUT_MS: int = Field(
        default=2000, ge=0,
        description="표 · 행 · advisory 잠금을 기다리는 상한(ms). 수신자 저장 잠금(stores._PROFILE_LOCK)도 여기에 걸린다")
    DB_IDLE_IN_TX_TIMEOUT_MS: int = Field(
        default=60000, ge=0,
        description="트랜잭션을 연 채 쉬는 세션을 끊는 상한(ms). 앱은 트랜잭션을 짧게만 연다 — 안전망")

    # ---- 카탈로그
    CATALOG_SOURCE: Literal["file", "db"] = Field(
        default="file",
        description="활성 카탈로그를 어디서 읽나. db = ai_catalog — **배포는 이쪽**(컨테이너에 파일이 없다). "
                    "file = CATALOG_FILE 의 JSON — 로컬 개발·시험 기본값",
    )
    CATALOG_FILE: Path = Field(
        default=Path("tests/fixtures/catalog_sample.json"),
        description="카탈로그 JSON(7.9 export 형식 또는 동료 공유본 원형). 기본은 레포 안 가짜 예시 111건 — 실제 카탈로그는 DB(CATALOG_SOURCE=db)로. "
                    "상대경로는 실행 위치(profiling/) 기준. CATALOG_SOURCE=file 일 때만 쓴다",
    )

    CATALOG_POLL_TTL_S: float = Field(
        default=1.0, ge=0,
        description="DbCatalogReader 가 활성 버전 id 를 다시 묻기까지의 최소 간격(초). 그 안에서는 DB 없이 들고 있던 카탈로그를 준다 — "
                    "7.6 한 건이 접수·슬롯에서 두 번 폴링해 DB 왕복 8번을 쓰던 것을 없앤다. 새 적재·활성 해제(503)가 보이기까지 "
                    "최대 이 시간만큼 늦는다. 0 = 호출마다 묻는다(옛 동작). CATALOG_SOURCE=db 일 때만 쓴다",
    )

    # ---- 계약 상한 (문서 1 §7.7). 7.6 의 비선호 5·리뷰 10 은 schemas.py 의 max_length 가 정본 — 설정으로 두지 않는다(09-29, 아무 데서도 안 읽었다)
    POOL_SIZE: int = Field(default=30, ge=1, le=30, description="7.7 recommendedProductIds 개수 상한")

    # ---- 업무 규칙 스위치 (10-11 결정). v1 = exclude, v2(BE V2 배포 — giftPreference·reviews) 부터 penalty 로 바꾼다
    DISLIKE_MODE: Literal["exclude", "penalty"] = Field(
        default="exclude",
        description="비선호 카테고리 처리 — exclude: 풀에서 뺀다(v1 하드 규칙, 다시 쓸 수 있게 남김) · "
                    "penalty: 빼지 않고 순서에서 감점(v2, 설계 DR-005). pipeline.DislikeMode · DISLIKED_CATEGORY_SCORE 참고",
    )

    POOL_STRATEGY: Literal["d", "s0"] = Field(
        default="d",
        description="순위에서 30개 고르기 — d: 비선호 하드 제외 · 한 소분류 최대 10 · 같은 모델 하나(10-12 결정, selection.select_pool) · "
                    "s0: 순위 앞 30개 그대로(10-12 까지의 규칙, 되돌릴 수 있게 남김). pipeline.PoolStrategy 참고",
    )

    # ---- 태그 추출(LLM) · 임베딩 (10-12, 기본 꺼짐 — 켜면 D 의 임베딩 단계가 돈다)
    TAGGER: Literal["none", "ollama", "openrouter"] = Field(
        default="none",
        description="태그 추출기 — none: 끔(태그 없이 D) · ollama: 로컬 Ollama(OLLAMA_URL · TAGGER_MODEL) · "
                    "openrouter: 외부 API(OPENROUTER_API_KEY · TAGGER_MODEL 예 anthropic/…). 실패하면 태그 없이 진행",
    )
    OPENROUTER_API_KEY: SecretStr | None = Field(
        default=None,
        description="TAGGER=openrouter 일 때 API 키 — profiling/.env(gitignore)에만 둔다. 로그 · 응답에 찍히지 않게 SecretStr",
    )
    TAG_PROMPT_FILE: str | None = Field(
        default=None,
        description="태그 추출 시스템 프롬프트 파일(문구 실험용 덮어쓰기) — 없으면 TAG_SPEC 규격의 src/profiling/prompts/<규격>_system.md. "
                    "축은 TAG_SPEC 을 따르고, 파일에 적힌 축이 다르면 기동 실패. 상대 경로는 실행 위치(profiling/) 기준. 바꾸면 판 이름(해시)이 바뀐다",
    )
    TAG_SPEC: Literal["tag4", "tag3", "tag3disliked"] = Field(
        default="tag3disliked",                  # v2 운영 규격 — 3축 + 비선호(DR-043, 10-16). Task Definition 에서 빠뜨려도 4축이 조용히 켜지지 않게(10-15)
        description="태그 규격 — 축 · 시스템 프롬프트 · 스키마 · 판 이름 한 묶음(tagging.TagSpec). tag4: 카테고리 · IP · 브랜드 · 특징 "
                    "(prompts/tag4_system.md) · tag3: 카테고리 · IP · 브랜드(prompts/tag3_system.md, DR-038) · "
                    "tag3disliked: tag3 + 비선호(v2 운영 — DR-043, 글 속 '싫다'를 비선호 태그로 뽑아 후보에서 빼고 disliked_tags 에 저장). "
                    "모르는 이름이면 기동 실패. 바꾸면 서버를 다시 켠다",
    )
    OPENROUTER_URL: str = Field(default="https://openrouter.ai/api/v1", description="OpenRouter API 주소(OpenAI 호환 /chat/completions)")
    OLLAMA_URL: str = Field(default="http://localhost:11434", description="TAGGER=ollama 일 때 Ollama 주소")
    TAGGER_MODEL: str | None = Field(
        default=None,
        description="태그 추출 모델. 비우면 추출기별 기본값(DEFAULT_TAGGER_MODEL) — openrouter: google/gemini-3.5-flash-lite(10-14 확정) · "
                    "ollama: gemma4:12b. 다른 모델은 이름을 직접(ollama 예 qwen3.5:9b, openrouter 는 모델 목록의 id)",
    )
    TAGGER_TIMEOUT_S: float = Field(default=120.0, gt=0, description="LLM 호출 한 번의 시간 상한(초)")
    EMBEDDING_DIR: str | None = Field(
        default=None,
        description="질의 인코더가 있는 product-search 폴더(embedding/ — Node · 모델). 없으면 임베딩 끔",
    )
    EMBEDDING_SOURCE: Literal["db", "file"] = Field(
        default="db",
        description="상품 벡터를 어디서 읽나 — db: ai_catalog.products.embedding(pgvector, 0009 · load_catalog --embeddings 로 적재) · "
                    "file: EMBEDDING_DIR 의 data/ 파일(검색기가 만든 manifest.json · vectors.f32)",
    )

    # ---- 실행 (3단계 구현 상세 §12.1 개발 시험 시작값)
    PROFILING_SLOTS: int = Field(
        default=1, ge=1,
        # 필드 이름에 접두사가 이미 들어 있어 env_prefix 를 태우면 PROFILING_PROFILING_SLOTS 가 된다(이슈 2026-09-28_1424).
        # 별칭은 접두사를 타지 않으므로 문서 이름 PROFILING_SLOTS 를 읽고, 옛 이름도 계속 받는다.
        validation_alias=AliasChoices("PROFILING_SLOTS", "PROFILING_PROFILING_SLOTS"),
        description="프로파일링 동시 실행 수 (Supervisor 슬롯). 환경변수 PROFILING_SLOTS",
    )
    RUNNING_STALE_S: int = Field(
        default=300, ge=1,
        description="RUNNING 기록을 '죽은 실행'으로 보는 기준(초). 접수 단계 중복 판정이 쓴다 — 이보다 오래된 RUNNING은 "
                    "프로세스가 죽어 남은 행으로 보고 다시 분석한다. Backend 의 복구 전송 간격(maximum-window ⚙6h)보다 훨씬 짧아야 "
                    "복구 전송이 '이미 분석 중'에 막히지 않고 재분석으로 이어진다(v0.7 ⑤)",
    )

    # ---- 운영
    RETRY_AFTER_S: int = Field(
        default=300, ge=1,
        description="503(활성 카탈로그 없음) 응답에 실어 보내는 Retry-After 초. Backend 는 이 헤더를 읽지 않는다"
                    "(통합 수정점 v0.7, 10-08 실측) — 운영자·다른 호출자용 표준 힌트로만 남긴다",
    )
    QUEUE_MAX: int = Field(
        default=200, ge=1,
        description="접수 대기열 상한(건). 슬롯이 다 찼을 때 큐에 둘 수 있는 수. 넘치면 7.6 을 503 으로 거절하는데, Backend 는 503 을 "
                    "재시도 가능 실패로 보고 틱을 끝내며 디바운스를 1회만 재시작한다(두 번째면 접음, v0.7 ②③). 그래서 BE 틱당 "
                    "일반 ⚙100 + 복구 ⚙50 = 150 보다 커야 한다(BE 도 150 ≤ 200 을 검증). 대기는 큐에서 하므로 스레드풀 토큰을 쓰지 않는다(09-28)",
    )
    QUEUE_FULL_RETRY_AFTER_S: int = Field(
        default=30, ge=1,
        description="대기열 가득 503 에 싣는 Retry-After 초. 카탈로그 없음 503 의 RETRY_AFTER_S(300)와 다르다 — 곧 빠지는 상황이라 짧게",
    )
    IO_THREADS: int = Field(
        default=4, ge=1,
        description="접수의 카탈로그 폴링과 /health 의 DB 문장을 돌리는 별도 스레드 수(anyio CapacityLimiter). "
                    "기본 스레드풀(40)과 분리해 실행 대기가 쌓여도 접수·/health 가 굶지 않는다",
    )
    SHUTDOWN_DRAIN_S: float = Field(
        default=5.0, ge=0,
        description="종료 시 실행 중인 분석을 기다리는 최대 초. 큐에 남은 것은 버린다(Backend 가 maximum-window 뒤 같은 번호로 "
                    "복구 전송). compose 종료 유예 10초보다 짧게",
    )
    LOG_LEVEL: str = Field(default="INFO", description="logging 레벨 이름")
    STRICT_STARTUP: bool = Field(
        default=False,
        description="서버(개발 · 운영 ECS) 기동 검사 — true 면 다음일 때 **기동을 거부**한다: DB 마이그레이션 판이 코드가 기대하는 판"
                    "(main.EXPECTED_MIGRATION)보다 낮음 · SERVICE_TOKEN 이 비었거나 공개 기본값(dev-token) · DATABASE_URL 비밀번호가 "
                    "공개 기본값 · DATABASE_URL 계정 이름이 공개 기본값(로컬 이름) · BACKEND_BASE_URL 이 로컬 가짜 BE 기본값 · CATALOG_SOURCE 가 db 가 아님. "
                    "로컬 개발 · compose · 시험은 false(기본) — 기본값으로 돌기 때문. Task Definition 에서 true(10-15)",
    )

    @property
    def tagger_model(self) -> str | None:
        """실제로 쓸 태그 추출 모델 — TAGGER_MODEL 이 있으면 그것, 없으면 추출기별 기본값. 추출기가 꺼져 있으면 None."""
        if self.TAGGER == "none":
            return None
        return self.TAGGER_MODEL or DEFAULT_TAGGER_MODEL[self.TAGGER]


def read_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """.env 파일의 `이름=값` 줄을 읽는다(없으면 빈 dict). 앱 설정이 아닌 값(마이그레이션의 PROFILING_APP_DB_*)을
    셸에 올리지 않고도 같은 .env 에서 읽으려고 둔다 — alembic/env.py · 시험 conftest 가 쓴다. 값은 어디에도 출력하지 않는다."""
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스당 1회만 읽는다. FastAPI에서는 Depends(get_settings)로, 나머지는 직접 호출.
    테스트에서 값을 바꾸려면 get_settings.cache_clear() 후 환경변수를 바꾸거나, Settings(...)를 직접 만들어 넘긴다."""
    return Settings()
