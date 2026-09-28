"""stores.run_lock 주변 — DB 없이 볼 수 있는 것 (이슈 2026-09-27_1633 B·C).

B: 잠금 키가 수신자 ID 를 32비트로 자르지 않는지.
C: 잠금을 걸지 못해도(커넥션·질의 어느 쪽이든) 진행하는지 — 잠금은 중복을 줄이는 장치이지 접수를 막는 장치가 아니다.
"""

import psycopg
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

    def execution_options(self, **kw):
        return self

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


# ---------------------------------------------------------------- D. AUTOCOMMIT (09-28 왕복 줄이기)


class _Scalar:
    def __init__(self, v):
        self._v = v

    def scalar_one(self):
        return self._v


class _RecordingConn:
    """잠금이 걸리는 커넥션 — 무엇을 어떤 순서로 불렀는지 남긴다."""

    def __init__(self):
        self.calls: list[str] = []
        self.options: dict = {}
        self.closed = False

    def execution_options(self, **kw):
        self.options.update(kw)
        self.calls.append("execution_options")
        return self

    def execute(self, stmt, params=None):
        self.calls.append(str(stmt))
        return _Scalar(True)

    def commit(self):
        self.calls.append("commit")

    def invalidate(self):
        self.calls.append("invalidate")

    def close(self):
        self.closed = True


class _RecordingEngine:
    def __init__(self):
        self.conn = _RecordingConn()

    def connect(self):
        return self.conn


class _NoAutocommitConn(_FailingConn):
    """execution_options 자체가 죽는 커넥션 — SQLAlchemy 가 감싸지 않는 psycopg 원본 오류."""

    def execution_options(self, **kw):
        raise psycopg.ProgrammingError("can't change autocommit state: connection in transaction status INTRANS")


def test_run_lock_uses_autocommit_and_never_commits() -> None:
    """잠금 커넥션은 AUTOCOMMIT — 트랜잭션을 열지 않으니 커밋도 없다(왕복 6 → 2). execute 보다 먼저 걸어야 한다."""
    engine = _RecordingEngine()
    with DbProfileRunStore(engine).run_lock(1, 1) as got:      # type: ignore[arg-type]
        assert got is True
    conn = engine.conn
    assert conn.options == {"isolation_level": "AUTOCOMMIT"} and conn.calls[0] == "execution_options"
    assert [c for c in conn.calls if "advisory" in c] == ["select pg_try_advisory_lock(:k)", "select pg_advisory_unlock(:k)"]
    assert "commit" not in conn.calls and conn.closed is True


def test_run_lock_proceeds_when_autocommit_cannot_be_set() -> None:
    engine = _FailingEngine()
    engine.conn = _NoAutocommitConn()
    with DbProfileRunStore(engine).run_lock(1, 1) as got:      # type: ignore[arg-type]
        assert got is True
    assert engine.conn.closed is True
