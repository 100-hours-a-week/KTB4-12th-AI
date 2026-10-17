"""DB 접속 설정(10-13 DB 검토) — 풀 크기 · 시간 상한 · 저장 잠금 키. DB 없이 볼 수 있는 것만(.env 는 읽지 않는다).

풀: 슬롯마다 잠금 커넥션 1개를 분석 내내 쥐고 접수 폴링 · /health 가 IO_THREADS 만큼 더 쓴다 → 기본 = 슬롯 + IO_THREADS, 모자라면 기동 거부.
"""

import pytest

from profiling import main
from profiling.settings import Settings
from profiling.stores import _lock_key, _profile_lock_key


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_pool_defaults_to_slots_plus_io_threads() -> None:
    assert main.db_pool_size(_s()) == 1 + 4
    assert main.db_pool_size(_s(PROFILING_SLOTS=8, IO_THREADS=4)) == 12
    assert main.db_pool_size(_s(PROFILING_SLOTS=8, DB_POOL_SIZE=20)) == 20


def test_too_small_pool_refuses_to_start_before_connecting() -> None:
    s = _s(PROFILING_SLOTS=8, DB_POOL_SIZE=6, DB_MAX_OVERFLOW=2, DATABASE_URL="postgresql+psycopg://x:y@127.0.0.1:1/none")
    with pytest.raises(RuntimeError, match="풀이 작다"):        # 6 + 2 < 8 + 4 — 접속을 시도하기 전에 거부
        main._connect_db(s)


def test_session_options_carry_the_three_timeouts() -> None:
    opts = main.db_session_options(_s(DB_STATEMENT_TIMEOUT_MS=5000, DB_LOCK_TIMEOUT_MS=2000, DB_IDLE_IN_TX_TIMEOUT_MS=0))
    assert opts == "-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=0"


def test_superuser_is_allowed_locally_by_default() -> None:
    assert _s().DB_REFUSE_SUPERUSER is False                    # 로컬 uv 는 소유자 계정 — 경고만. compose · 서버는 true


def test_profile_save_lock_key_differs_from_run_lock_keys() -> None:
    """수신자 저장 잠금(이슈 2026-10-13_1706)이 실행 잠금 (수신자, 버전) 과 같은 키가 되면 run_lock 을 쥔 스레드가 서로를 막는다."""
    rid = 9871
    assert _profile_lock_key(rid) == _profile_lock_key(rid) and -(2**63) <= _profile_lock_key(rid) < 2**63
    assert _profile_lock_key(rid) not in {_lock_key(rid, sv) for sv in range(1, 200)}
    assert _profile_lock_key(rid) != _profile_lock_key(rid + 2**32)
