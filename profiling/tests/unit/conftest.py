"""단위 테스트 공통 — 외부 의존 없이 돈다: 저장소는 메모리(PROFILING_STORE=memory). DB가 필요한 시험은 tests/integration."""

import os

os.environ.setdefault("PROFILING_STORE", "memory")   # 앱(main.lifespan)이 DB에 붙지 않게. 통합 테스트는 자기 fixture에서 db로 바꾼다
