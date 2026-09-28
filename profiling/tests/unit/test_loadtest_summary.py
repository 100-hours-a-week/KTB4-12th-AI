"""tools/loadtest/run.py 의 집계 함수 — 네트워크·DB 없이 표본으로만 검사한다."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.loadtest.run import (
    DbSample,
    Post,
    Probe,
    _parse_args,
    arrival_stats,
    dist,
    markdown,
    parse_ai_log,
    pct,
    summarize,
)

T0 = datetime(2026, 9, 28, 5, 0, 0, tzinfo=UTC)


def _row(rid: int, status: str, start_s: float, end_s: float, attempts: int = 1) -> dict:
    return {"recipient_user_id": rid, "source_version": 1, "status": status, "attempt": 1, "callback_attempts": attempts,
            "created_at": T0 + timedelta(seconds=start_s), "updated_at": T0 + timedelta(seconds=end_s)}


def test_pct_and_dist() -> None:
    values = [float(i) for i in range(1, 101)]
    assert pct(values, 50) == 50.5
    assert abs(pct(values, 95) - 95.05) < 1e-9
    assert pct([], 50) is None
    assert pct([7.0], 95) == 7.0
    assert dist([]) == {"n": 0, "p50": None, "p95": None, "max": None}
    assert dist([3.0, 1.0, 2.0])["max"] == 3.0


def test_summarize_counts_everything() -> None:
    posts = [Post(900001, 0.00, 20.0, 202), Post(900002, 0.01, 30.0, 202), Post(900003, 0.02, 40.0, 202),
             Post(900004, 0.03, 12_000.0, 202), Post(900005, 0.04, 30_000.0, None, "ReadTimeout")]
    probes = [Probe(0.0, 5.0, 200, None, 0, 0, 0), Probe(0.3, 1500.0, 200, None, 1, 5, 0),
              Probe(2.0, 10_000.0, None, "ReadTimeout", None, None, None), Probe(3.0, 6.0, 200, None, 0, 5, 1)]
    db = [DbSample(0.5, 3, 0, 1, 0), DbSample(1.5, 4, 1, 1, 2), DbSample(3.0, 2, 0, 0, 4)]
    rows = [_row(900001, "DELIVERED", 0.10, 0.14), _row(900002, "DELIVERED", 0.15, 0.20),
            _row(900003, "RESULT_READY", 0.21, 2.80, attempts=3), _row(900004, "DELIVERED", 12.0, 12.05),
            _row(900004, "DELIVERED", 12.1, 12.2)]                       # 같은 수신자 2행 (재전송 흔적)
    s = summarize(posts, probes, db, rows, n=5, threshold_ms=1000, t0_db=T0,
                  health_before={"supervisor": {"submitted": 10}}, health_after={"supervisor": {"submitted": 15}},
                  received_before=2, received_after=6)
    assert s["post"]["status_counts"] == {"202": 4, "ReadTimeout": 1}
    assert s["post"]["ms"]["n"] == 4 and s["post"]["over_10s"] == 2
    assert s["health"]["failed"] == 1 and s["health"]["over_threshold"] == 2
    assert s["health"]["worst"][0]["ms"] == 10_000.0 and s["health"]["undelivered_max"] == 1
    assert s["db"] == {"connections_max": 4, "idle_in_tx_max": 1, "waiting_max": 2}   # 0.5s: 응답 받은 3 − 종료 0 − RUNNING 1
    assert s["runs"]["by_status"] == {"DELIVERED": 4, "RESULT_READY": 1}
    assert s["runs"]["recipients_terminal"] == 4 and s["runs"]["complete"] is False
    assert s["runs"]["time_to_all_terminal_s"] == 12.2
    assert abs(s["runs"]["slot_ms"]["max"] - 2590.0) < 1e-6
    assert s["runs"]["callback_attempts_total"] == 7 and s["runs"]["multi_row_recipients"] == 1
    assert s["supervisor_submitted_delta"] == 5 and s["received_delta"] == 4
    assert abs(s["runs"]["e2e_ms"]["p50"] - 2780.0) < 1e-6     # 접수→종료 [140, 190, 2780, 12020, 12170] 의 중앙값
    md = markdown(s)
    assert md.startswith("| 항목 | 값 |") and "10초 초과 2" in md and "미완료" in md


def test_summarize_empty_inputs() -> None:
    s = summarize([], [], [], [], n=3, threshold_ms=1000, t0_db=T0, health_before=None, health_after=None,
                  received_before=0, received_after=0)
    assert s["runs"]["complete"] is False and s["runs"]["throughput_per_s"] is None
    assert s["supervisor_submitted_delta"] is None and s["db"]["waiting_max"] is None
    assert "| 완료 |" in markdown(s)


def test_parse_ai_log_and_arrivals(tmp_path: Path) -> None:
    lines = ["2026-09-28 14:00:00,100 INFO profiling.intake: 7.6 접수 recipient=900001 source_version=1 disliked=1 reviews=0 pref=False",
             "2026-09-28 14:00:00,120 INFO profiling.intake: 7.6 접수 recipient=900002 source_version=1 disliked=1 reviews=0 pref=False",
             "2026-09-28 14:00:00,130 INFO profiling.intake: 7.6 접수 recipient=1 source_version=3 disliked=0 reviews=0 pref=False",
             "2026-09-28 14:00:10,500 INFO profiling.intake: 7.6 접수 recipient=900003 source_version=1 disliked=1 reviews=0 pref=False",
             "2026-09-28 14:00:10,700 INFO profiling.intake: 7.7 콜백 결과 recipient=900001 source_version=1 시도 1/3 → DELIVERED"]
    p = tmp_path / "ai.log"
    p.write_text("\n".join(lines), encoding="utf-8")
    arrivals = parse_ai_log(p, 900001, 900010)
    assert [a[1] for a in arrivals] == [900001, 900002, 900003] and arrivals[0][0].tzinfo is not None
    stats = arrival_stats(arrivals)
    assert stats["n"] == 3 and [c["size"] for c in stats["clusters"]] == [2, 1]
    assert stats["max_per_second"] == 2 and abs(stats["gap_ms"]["max"] - 10_380.0) < 1e-6
    assert arrival_stats([])["clusters"] == []


def test_no_keepalive_flag_parses_and_defaults_off() -> None:
    """--no-keepalive: 요청마다 새 연결(Connection: close). 기본은 keep-alive — 09-28 결과와 비교할 때 그대로 두고, 서버 접수 능력을 잴 때 켠다."""
    assert _parse_args(["--no-keepalive"]).no_keepalive is True
    assert _parse_args([]).no_keepalive is False
