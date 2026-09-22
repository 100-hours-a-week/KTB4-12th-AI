"""설정 — 환경변수 → 객체 하나. 실험 config.json의 값 중 서비스에 필요한 것만 옮긴다.

읽는 순서(pydantic-settings): 환경변수 > .env 파일 > 아래 기본값. 접두사 PROFILING_ 을 붙인 이름만 읽는다
(예: PROFILING_BACKEND_BASE_URL). 접두사와 이름은 팀원 골격(DATABASE_URL·INTERNAL_SERVICE_TOKEN·MAIN_BACKEND_URL,
접두사 없음)과 다르므로 합칠 때 여기 한 파일만 바꾼다 — 코드는 settings.BACKEND_BASE_URL 처럼 속성으로만 쓴다.

값의 출처
  - POOL_SIZE 30 · MAX_REVIEWS 10 : 모델 API 설계 §7.6·§7.7
  - MAX_DISLIKED 5                : 화면 설계 C-05 (문서 1에는 상한 없음 — 결정 b)
  - CALLBACK_TIMEOUT_S 5          : 실험 하네스 config.json timeout_s(120)은 LLM용. 콜백은 짧게
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서비스 설정. 대문자 필드명 = 환경변수 이름에서 접두사를 뗀 것 (PROFILING_POOL_SIZE → POOL_SIZE)."""

    model_config = SettingsConfigDict(
        env_prefix="PROFILING_",
        env_file=".env",            # 실행 위치(profiling/) 기준. 없어도 된다
        env_file_encoding="utf-8",
        extra="ignore",             # .env에 다른 서비스 변수가 섞여 있어도 무시
    )

    # ---- 바깥 연결
    BACKEND_BASE_URL: str = Field(default="http://localhost:8081", description="7.7 콜백·7.9 export 대상. 로컬은 tools/fake_backend")
    SERVICE_TOKEN: str = Field(default="", description="Backend↔AI 서비스 토큰(Bearer). 비어 있으면 7.6 토큰 검사 생략(로컬 전용)")
    CALLBACK_TIMEOUT_S: float = Field(default=5.0, gt=0, description="7.7 POST 한 번의 타임아웃(초)")

    # ---- DB (로컬 compose 기본값. 팀원 골격 이름은 DATABASE_URL — 합칠 때 접두사만 맞춤)
    DATABASE_URL: str = Field(default="postgresql+psycopg://ai_user:ai_password@localhost:5432/ai_chat",
                              description="SQLAlchemy URL (psycopg 3). Alembic env.py도 이 값을 쓴다")
    STORE: Literal["db", "memory"] = Field(
        default="db",
        description="실행 기록·수신자 프로필 저장소. db = PostgreSQL(profile_runs·recipient_profiles, 시작 시 연결 확인 — 실패하면 앱이 뜨지 않음) · "
                    "memory = 프로세스 메모리(단위 테스트·DB 없는 로컬용. 수신자 프로필은 저장하지 않음)",
    )

    # ---- 카탈로그 (DB adapter 전까지 파일)
    CATALOG_FILE: Path = Field(
        default=Path("tests/fixtures/catalog_sample.json"),
        description="카탈로그 JSON(7.9 export 형식 또는 동료 공유본 원형). 기본은 레포 안 예시 111건 — 전체 4,231건은 .env에서 지정. "
                    "상대경로는 실행 위치(profiling/) 기준. DB 전환 후 삭제",
    )

    # ---- 계약 상한 (문서 1 §7.6·§7.7 · C-05)
    POOL_SIZE: int = Field(default=30, ge=1, le=30, description="7.7 recommendedProductIds 개수 상한")
    MAX_DISLIKED: int = Field(default=5, ge=0, description="dislikedCategories 상한 (C-05)")
    MAX_REVIEWS: int = Field(default=10, ge=0, description="reviews 상한 (7.6: 최신순 최대 10)")

    # ---- 실행 (3단계 구현 상세 §12.1 개발 시험 시작값)
    PROFILING_SLOTS: int = Field(default=1, ge=1, description="프로파일링 동시 실행 수 (Supervisor 슬롯)")

    # ---- 운영
    LOG_LEVEL: str = Field(default="INFO", description="logging 레벨 이름")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스당 1회만 읽는다. FastAPI에서는 Depends(get_settings)로, 나머지는 직접 호출.
    테스트에서 값을 바꾸려면 get_settings.cache_clear() 후 환경변수를 바꾸거나, Settings(...)를 직접 만들어 넘긴다."""
    return Settings()
