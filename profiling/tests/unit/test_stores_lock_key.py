"""stores.run_lock 주변 — DB 없이 볼 수 있는 것 (이슈 2026-09-27_1633 B·C).

B: 잠금 키가 수신자 ID 를 32비트로 자르지 않는지.
C: 잠금을 걸지 못해도(커넥션·질의 어느 쪽이든) 진행하는지 — 잠금은 중복을 줄이는 장치이지 접수를 막는 장치가 아니다.
"""

import sqlalchemy as sa

from profiling.stores import DbProfileRunStore, _lock_key

# ---------------------------------------------------------------- B. 잠금 키


def test_lock_key_does_not_truncate_recipient_id() -> None:
    """고치기 전에는 rid 와 rid + 2^32 가 같은 키였다 — 서로 다른 수신자가 서로의 접수를 막았다."""
    assert _lock_key(5, 1) != _lock_key(5 + 2**32, 1)
    assert _lock_key(5, 1) != _lock_key(5, 1 + 2**32)


def test_lock_key_is_stable_and_fits_bigint() -> None:
    a, b = _lock_key(9871, 3), _lock_key(9871, 3)
    assert a == b                                               # 같은 입력 → 같은 키 (프로세스가 달라도)
    assert -(2**63) <= a < 2**63                                # pg_try_advisory_lock(bigint) 범위
    assert _lock_key(9871, 3) != _lock_key(9871, 4) != _lock_key(9872, 3)


# ---------------------------------------------------------------- C. 못 걸면 진행


class _FailingConn:
    """connect() 는 되지만 질의가 죽는 커넥션 — DB 가 잠금만 못 거는 상황."""

    def __init__(self):
        self.closed = False

    def execute(self, *a, **k):
        raise sa.exc.OperationalError("select pg_try_advisory_lock", {}, Exception("connection reset"))

    def commit(self):
        pass

    def close(self):
        self.closed = True


class _FailingEngine:
    def __init__(self):
        self.conn = _FailingConn()

    def connect(self):
        return self.conn


class _DeadEngine:
    def connect(self):
        raise sa.exc.OperationalError("connect", {}, Exception("refused"))


def test_run_lock_proceeds_when_lock_query_fails() -> None:
    """고치기 전에는 질의 실패가 raise 로 새어 나가 dispatch 가 조용히 죽었다(202 는 이미 돌려준 뒤)."""
    engine = _FailingEngine()
    with DbProfileRunStore(engine).run_lock(1, 1) as got:      # type: ignore[arg-type]
        assert got is True
    assert engine.conn.closed is True                           # 실패한 커넥션도 닫는다


def test_run_lock_proceeds_when_connect_fails() -> None:
    with DbProfileRunStore(_DeadEngine()).run_lock(1, 1) as got:   # type: ignore[arg-type]
        assert got is True
