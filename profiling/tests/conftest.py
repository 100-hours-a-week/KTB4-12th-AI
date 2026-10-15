"""시험 공통 — 개발자의 profiling/.env 가 시험 결과를 바꾸지 않게 한다.

시험은 코드 기본값을 기준으로 쓰였다. .env 에 서비스 토큰 · 추출기 · 슬롯 같은 값이 있으면 그 값이 새어 들어와 시험이 깨진다.
그래서 DB 시험이 쓸 두 값(DB 주소 · 앱 계정 이름)만 .env 에서 받고, 나머지는 .env 를 읽지 않는다. 셸 환경변수는 그대로 우선한다.
"""

import os
from pathlib import Path

from profiling.settings import Settings, read_dotenv

_KEEP = ("PROFILING_DATABASE_URL", "PROFILING_APP_DB_ROLE")

for _k, _v in read_dotenv(Path(__file__).resolve().parents[1] / ".env").items():
    if _k in _KEEP and _v:
        os.environ.setdefault(_k, _v)

Settings.model_config["env_file"] = None
