"""설정 — 환경변수 → 객체 하나. 실험 config.json의 값 중 서비스에 필요한 것만 옮긴다.

읽는 순서(pydantic-settings): 환경변수 > .env 파일 > 아래 기본값. 기본적으로 PROFILING_ 접두사를 붙인다
(예: PROFILING_POOL_SIZE). 모델 키는 OPENROUTER_API_KEY_PROFILER 와 기존 PROFILING_OPENROUTER_API_KEY 를 읽는다.
같은 출처에 둘 다 있으면 새 이름이 우선한다(빈 값도 명시값). _FILE 및 통합 자격 증명 충돌 검사는 integration 이 맡는다.
팀원 골격은 아직 없다(09-18 초기 골격은 되돌려짐) — 합칠 때 접두사·이름을 맞추더라도
여기 한 파일만 바꾼다. 코드는 settings.POOL_SIZE 처럼 속성으로만 쓴다.

값의 출처
  - POOL_SIZE 30                  : 모델 API 설계 §7.7 · BE MQ 설계 결과 메시지 recommendedProductIds 최대 30
  - BE ↔ AI 프로파일링 요청 · 결과는 RabbitMQ(MQ_*) — 11-08 HTTP 7.6 · 7.7 과 그 설정(BACKEND_BASE_URL · CALLBACK_* · PROFILING_SLOTS ·
    QUEUE_* · RUNNING_STALE_S · SHUTDOWN_DRAIN_S)을 지웠다. 남은 환경변수는 extra="ignore" 라 기동을 막지 않는다
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
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
        populate_by_name=True,       # 별칭 추가 후에도 Settings(OPENROUTER_API_KEY=...) 호출 호환
    )

    # ---- 바깥 연결
    SERVICE_TOKEN: str = Field(default="", description="서비스 토큰(Bearer) — 챗봇의 프로필 조회 API(profile_read) 인증 · 검색 API 토큰이 없을 때 대신. 비어 있으면 검사 생략(로컬 전용)")

    # ---- DB (로컬 compose 기본값. 합칠 때 접두사만 맞춤)
    DATABASE_URL: str = Field(default="",                # 기본값 없음(10-15) — .env 또는 환경변수로. 비면 기동 · 마이그레이션이 멈춘다
                              description="SQLAlchemy URL (psycopg 3). Alembic env.py도 이 값을 쓴다. 앱은 운영 · 개발 서버에서 "
                                          "앱 전용 계정(ai_profile 읽기·쓰기, ai_catalog 읽기만, 이름은 PROFILING_APP_DB_ROLE)으로, 마이그레이션 · 적재는 소유자 계정으로")
    DB_REFUSE_SUPERUSER: bool = Field(
        default=False,
        description="true 면 앱이 슈퍼유저 계정으로 접속할 때 기동을 거부한다(운영 · 개발 서버 · compose 는 true + 앱 전용 계정). "
                    "false(로컬 uv 기본)면 경고 로그만")
    # 커넥션 풀(10-13 DB 검토) — MQ 슬롯마다 잠금 커넥션 2개를 분석 내내 쥐고(mq.runtime.db_connections), /health · 조회 API 가 IO_THREADS 만큼 더 쓴다
    DB_POOL_SIZE: int | None = Field(
        default=None, ge=1,
        description="상시 커넥션 수. 비우면 IO_THREADS + MQ 잠금(켰을 때). pool_size + max_overflow 가 그보다 작으면 기동 거부 "
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
    CATALOG_SOURCE: Literal["file", "db", "search_http"] = Field(
        default="file",
        description="활성 카탈로그를 어디서 읽나. search_http = 공용 Search API(통합 후보), "
                    "db = 기존 ai_catalog, file = 로컬 개발·시험 JSON",
    )
    CATALOG_FILE: Path = Field(
        default=Path("tests/fixtures/catalog_sample.json"),
        description="카탈로그 JSON(7.9 export 형식 또는 동료 공유본 원형). 기본은 레포 안 가짜 예시 111건 — 실제 카탈로그는 DB(CATALOG_SOURCE=db)로. "
                    "상대경로는 실행 위치(profiling/) 기준. CATALOG_SOURCE=file 일 때만 쓴다",
    )

    CATALOG_POLL_TTL_S: float = Field(
        default=1.0, ge=0,
        description="DbCatalogReader 가 활성 버전 id 를 다시 묻기까지의 최소 간격(초). 그 안에서는 DB 없이 들고 있던 카탈로그를 준다 — "
                    "요청 한 건이 두 번 폴링해 DB 왕복 8번을 쓰던 것을 없앤다. 새 적재·활성 해제(503)가 보이기까지 "
                    "최대 이 시간만큼 늦는다. 0 = 호출마다 묻는다(옛 동작). CATALOG_SOURCE=db 일 때만 쓴다",
    )

    # ---- 계약 상한 (문서 1 §7.7). 요청의 비선호 5·리뷰 10 상한은 mq/messages.py 가 정본 — 설정으로 두지 않는다(09-29)
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

    INCREMENTAL_TAGS: bool = Field(
        default=False,
        description="증분 태그 추출(10-29) — true: 이전 프로필의 태그 중 근거(취향 문장 · 리뷰)가 그대로인 것은 다시 쓰고 바뀐 글만 LLM 에 보낸다 · "
                    "false: 늘 전체 추출(지금까지). 이전 프로필 없음 · 판이 다름 · 이전 실패면 켜도 전체 추출. incremental.py 참고",
    )

    # ---- 태그 추출(LLM) · 임베딩 (10-12, 기본 꺼짐 — 켜면 D 의 임베딩 단계가 돈다)
    TAGGER: Literal["none", "ollama", "openrouter"] = Field(
        default="none",
        description="태그 추출기 — none: 끔(태그 없이 D) · ollama: 로컬 Ollama(OLLAMA_URL · TAGGER_MODEL) · "
                    "openrouter: 외부 API(OPENROUTER_API_KEY · TAGGER_MODEL 예 anthropic/…). 실패하면 태그 없이 진행",
    )
    OPENROUTER_API_KEY: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENROUTER_API_KEY_PROFILER", "PROFILING_OPENROUTER_API_KEY"),
        description="TAGGER=openrouter 일 때 API 키 — OPENROUTER_API_KEY_PROFILER(기존 PROFILING_OPENROUTER_API_KEY 호환). "
                    "같은 출처에서는 새 이름 우선. 로그 · 응답에 찍히지 않게 SecretStr",
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

    # ---- 태그 추출 대역(성능 측정 전용, 기본 꺼짐 — llm_stub.py). 모델 대신 정한 시간만큼 기다렸다 고정 답. 비용 0
    TAGGER_STUB: bool = Field(default=False, description="true 면 TAGGER=openrouter 의 전송만 대역으로 — 모델 호출 · 키 없음, "
                                                         "v3 엔진도 켜지 않음. 나머지(검증 · 검색 · 저장 · 전달)는 실제 코드")
    TAGGER_STUB_P50_S: float = Field(default=4.1, description="대역 응답 시간 중앙값(초) — 기본 v3 A100 8명 동시 실측(11-04)")
    TAGGER_STUB_P95_S: float = Field(default=6.9, description="대역 응답 시간 95번째 값(초) — 기본 v3 A100 8명 동시 실측(11-04)")
    TAGGER_STUB_JITTER: Literal["none", "lognormal", "uniform"] = Field(
        default="lognormal", description="흔들림 방식 — none: 늘 p50 · lognormal: p50 · p95 맞춘 로그정규 · uniform: p50 · p95 맞춘 균등")
    TAGGER_STUB_MAX_S: float | None = Field(default=12.8, description="대역 응답 시간 상한(초) — 기본 v3 실측 가장 느린 한 명. 비우면 자르지 않음")
    TAGGER_STUB_SEED: int | None = Field(default=None, description="뽑기 씨앗 — 주면 뽑는 순서가 매번 같다")

    @field_validator("TAGGER_STUB_MAX_S", "TAGGER_STUB_SEED", mode="before")
    @classmethod
    def _stub_blank_is_none(cls, v):
        """대역 상한 · 씨앗은 환경변수를 비워 두면 '없음'(자르지 않음 · 매번 다름)."""
        return None if isinstance(v, str) and not v.strip() else v

    # ---- v3 태그 엔진(11-03, 11-04 기본 v3 — 배포 대비). TAGGER 추출기는 v3 를 못 쓸 때의 폴백. V3_URL · 키 3개가 다 있어야 실제로 켜지고,
    #      하나라도 비면 기동 로그에 남기고 v2 추출기로만 돈다(로컬 · 시험은 그대로 v2). v2 로 되돌리려면 TAG_ENGINE=v2
    TAG_ENGINE: Literal["v2", "v3"] = Field(
        default="v3",
        description="태그 엔진 — v2: TAGGER 추출기 하나(v3 를 끌 때) · v3: Modal v3 컨테이너(V3_URL — 12B 나눠 뽑기 · 질문 판정 · Laya 거부)에 사람마다 한 번 + 규칙 A·B·C. "
                    "v3 를 못 쓰면(연결 · 5xx · V3_TIMEOUT_S 넘음 · 상대 실패) 그 사람은 TAGGER 추출기로(V3_FALLBACK). TAG_SPEC=tag3disliked 만",
    )
    V3_URL: str = Field(default="", description="Modal v3 컨테이너 주소(/v3/tags 앞까지). 비면 v3 를 켜지 않음")
    V3_API_KEY: SecretStr | None = Field(default=None, description="Modal v3 컨테이너 키(Bearer, 컨테이너 안에서 확인). 로그 · 응답에 찍히지 않게 SecretStr")
    V3_PROXY_KEY: SecretStr | None = Field(
        default=None,
        description="Modal 프록시 인증 토큰 ID(Modal-Key 헤더) — Modal 이 컨테이너를 켜기 전에 문 앞에서 확인(키 없는 요청이 GPU 를 깨우지 않게). "
                    "V3_PROXY_SECRET 과 둘 다 있어야 v3 를 켬",
    )
    V3_PROXY_SECRET: SecretStr | None = Field(default=None, description="Modal 프록시 인증 토큰 비밀(Modal-Secret 헤더)")
    V3_TIMEOUT_S: float = Field(
        default=240.0, gt=0,
        description="사람 한 명의 v3 기한(초) — 꺼진 Modal 컨테이너가 켜지는 시간(약 2분)을 넉넉히 기다린다. 넘으면 그 사람은 v2 폴백. "
                    "폴백까지 더해도 MQ_RECONCILE_AFTER_S 보다 짧아야 대조 복구가 분석 중인 건을 장기 미완료로 보지 않는다",
    )
    V3_FALLBACK: bool = Field(default=True, description="v3 를 못 쓸 때 TAGGER 추출기로 넘길지 — false 면 태그 없이 진행")
    SEARCH_BASE_URL: str = ""
    SEARCH_TOKEN: SecretStr | None = None

    EMBEDDING_DIR: str | None = Field(
        default=None,
        description="질의 인코더가 있는 product-search 폴더(embedding/ — Node · 모델). 없으면 임베딩 끔",
    )
    EMBEDDING_SOURCE: Literal["db", "file"] = Field(
        default="db",
        description="상품 벡터를 어디서 읽나 — db: ai_catalog.products.embedding(pgvector, 0009 · load_catalog --embeddings 로 적재) · "
                    "file: EMBEDDING_DIR 의 data/ 파일(검색기가 만든 manifest.json · vectors.f32)",
    )

    # ---- 운영
    RETRY_AFTER_S: int = Field(
        default=300, ge=1,
        description="503 응답에 실어 보내는 Retry-After 초 — 운영자 · 다른 호출자용 표준 힌트",
    )
    IO_THREADS: int = Field(
        default=4, ge=1,
        description="/health · /readyz · 조회 API 의 DB 문장을 돌리는 별도 스레드 수(anyio CapacityLimiter). "
                    "기본 스레드풀(40)과 분리해 분석이 쌓여도 /health 가 굶지 않는다",
    )

    # ---- RabbitMQ 요청 소비 (BE 「프로파일링 MQ — AI 소비·결과 발행 상세 설계」 §7 설정표)
    # 숫자는 설계가 정하지 않았다 — 배포 설정으로 넣고, 켰는데 빠졌거나 0 · 음수 · 서로 모순이면 기동을 거부한다(mq.config).
    MQ_ENABLED: bool = Field(default=False, description="요청 Consumer 켜기. 의존 리소스(브로커 · 큐 · 권한) 준비 전에는 끔")
    MQ_URL: SecretStr = Field(default=SecretStr(""), description="브로커 주소 + AI 계정(시크릿 — SecretStr 이라 설정을 출력해도 가려진다). 예 amqps://<계정>:<비밀번호>@<호스트>:5671/gift")
    MQ_PREFETCH: int | None = Field(default=None, description="브로커가 한 번에 넘겨줄 미확인(unacked) 메시지 수(basic.qos). 배포 전체 합계를 넘지 않게")
    MQ_SLOTS: int | None = Field(default=None, description="동시에 분석하는 수(Consumer 슬롯). 배포 전체 합산 용량을 넘지 않게")
    MQ_RECONNECT_DELAY_S: float | None = Field(default=None, description="브로커 연결이 끊긴 뒤 다시 연결하기까지 기다리는 초")
    MQ_SHUTDOWN_GRACE_S: float | None = Field(default=None, description="종료 때 진행 중인 메시지의 처리를 기다리는 최대 초. 넘으면 연결을 닫아 브로커가 재배달")
    MQ_CONFIRM_TIMEOUT_S: float | None = Field(default=None, description="결과 발행의 브로커 확인(Confirm)을 기다리는 최대 초. 넘으면 '성공 여부 불명' — 결과를 둔 채 같은 바이트를 다시 발행")
    MQ_WAIT_S: float | None = Field(default=None, description="제한 간격 대기 — 잠금을 못 얻었을 때 · AI DB 장애 · 결과 발행 실패 때 같은 메시지를 다시 시도하기까지의 초")
    MQ_MAX_ANALYSIS_ATTEMPTS: int | None = Field(default=None, description="한 요청의 분석 시도 예산(커밋된 시작 횟수, 재배달로 초기화 안 함). v3 → v2 → 태그 없는 추천 폴백은 한 번으로 센다")
    MQ_RETRY_BASE_S: float | None = Field(default=None, description="일시적인 분석 오류 뒤 첫 재시도까지의 기준 초. n번째 실패 뒤 대기 = min(MQ_RETRY_MAX_S, 기준 × 2^(n-1)) 의 절반 + 무작위 절반(지터). 다음 시각은 DB 에 저장")
    MQ_RETRY_MAX_S: float | None = Field(default=None, description="재시도 대기의 최대 초(MQ_RETRY_BASE_S 이상). 대기 중 메시지는 미확인(unacked)으로 쥐고 있으므로 브로커 consumer ACK timeout 보다 충분히 짧게")
    MQ_RECONCILE_AFTER_S: float | None = Field(default=None, description="대조 복구(설계 CO-06) — 이 초보다 오래 끝나지 않은(SETTLED 아님) 전달 기록을 '장기 미완료'로 본다. MQ_RETRY_MAX_S 보다 커야 하고, 분석 한 번 · 발행 재시도에 걸리는 시간보다 넉넉해야 한다(기준은 BE 와 공동 확정 전)")
    MQ_RECONCILE_INTERVAL_S: float | None = Field(default=None, description="대조 복구를 도는 간격(초)")

    LOG_LEVEL: str = Field(default="INFO", description="logging 레벨 이름")
    STRICT_STARTUP: bool = Field(
        default=False,
        description="서버(개발 · 운영 ECS) 기동 검사 — true 면 다음일 때 **기동을 거부**한다: DB 마이그레이션 판이 코드가 기대하는 판"
                    "(main.EXPECTED_MIGRATION)보다 낮음 · SERVICE_TOKEN 이 비었거나 공개 기본값(dev-token) · DATABASE_URL 비밀번호가 "
                    "공개 기본값 · DATABASE_URL 계정 이름이 공개 기본값(로컬 이름) · CATALOG_SOURCE 가 db 가 아님. "
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
