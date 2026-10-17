"""status — 외부 LLM 상태(/health 의 llm 칸). 프로세스 장애와 구분하려는 것이라 실패해도 요청은 계속되고, 상태만 바뀐다(10-15)."""

from profiling.status import DOWN_AFTER, LlmStatus


def test_off_when_not_enabled_and_reason_kept() -> None:
    s = LlmStatus()
    s.configure(requested="openrouter", enabled=False, model="m", reason="no_api_key")
    snap = s.snapshot()
    assert snap["state"] == "off" and snap["reason"] == "no_api_key" and snap["requested"] == "openrouter"


def test_unknown_then_ok_then_degraded_then_down_then_recovers() -> None:
    s = LlmStatus()
    s.configure(requested="openrouter", enabled=True, model="m")
    assert s.snapshot()["state"] == "unknown"
    s.record("ok")
    assert s.snapshot()["state"] == "ok"
    s.record("fail:timeout")
    snap = s.snapshot()
    assert snap["state"] == "degraded" and snap["last_error"] == "fail:timeout" and snap["consecutive_failures"] == 1
    for _ in range(DOWN_AFTER - 1):
        s.record("fail:http_429")
    assert s.snapshot()["state"] == "down"
    s.record("empty")                                   # 답은 받았다(검증기가 전부 버림) — 성공으로 친다
    snap = s.snapshot()
    assert snap["state"] == "ok" and snap["consecutive_failures"] == 0 and snap["calls"] == {"ok": 2, "fail": DOWN_AFTER}


def test_broken_output_counts_as_failure_and_skips_are_ignored() -> None:
    s = LlmStatus()
    s.configure(requested="openrouter", enabled=True, model="m")
    s.record("skip_no_signal")
    s.record("skip")
    assert s.snapshot()["state"] == "unknown"           # LLM 을 안 불렀으면 세지 않는다
    s.record("bad_json")
    assert s.snapshot()["state"] == "degraded"


def test_snapshot_has_no_raw_error_text() -> None:
    s = LlmStatus()
    s.configure(requested="openrouter", enabled=True, model="m")
    s.record("fail:http_401")
    assert set(s.snapshot()) == {"state", "requested", "enabled", "model", "reason", "last", "last_ok_at", "last_error",
                                 "last_error_at", "consecutive_failures", "calls"}
