"""GPU · 메모리 실시간 기록 — 언제 작업이 몰리고, 어떻게 GPU(통합 메모리)가 버거워지는지 보기 위한 모니터(10-13).

    cd profiling && uv run python -m tools.bench.gpu_monitor --interval 1 [--ai http://127.0.0.1:8000] [--log /tmp/be-console-ai.log] [--label q6]
    (끄기: Ctrl+C — 끝날 때 요약을 찍는다. 기록: tools/bench/out/gpu_<label>_<시각>.csv)
    uv run --with matplotlib python -m tools.bench.gpu_monitor --plot tools/bench/out/gpu_….csv     # 시간 그림 + 요약

Apple Silicon(맥, sudo 불필요)
  GPU      ioreg IOAccelerator PerformanceStatistics — Device/Renderer Utilization %, Alloc system memory(GPU 가 잡은 메모리),
           In use system memory, recoveryCount(**GPU 리셋 횟수 — 늘면 '터짐'**)
  메모리   sysctl kern.memorystatus_vm_pressure_level(1 정상 · 2 경고 · 4 위험) · vm.swapusage · vm_stat pageouts(초당 디스크로 내보낸 페이지)
  Ollama   /api/ps — 올라간 모델 · 크기 · size_vram → **CPU 로 밀려난 비율**(0 이 정상, 0 보다 크면 GPU 메모리 부족)
NVIDIA(서버) nvidia-smi 가 있으면 GPU 사용률 · 메모리 · 온도 · 전력을 같은 행에 더한다.
AI 서버   --ai 를 주면 /health 의 supervisor(실행 중 · 대기열 · 완료 · 실패)로 **작업이 몰리는 순간**을 같은 시간축에 남긴다.
로그      --log 를 주면 그 파일의 tag_extract 줄 수(누적)를 같이 센다 — 분석이 끝난 시점 표시.
CPU      전체 사용률(맥 host_statistics · 리눅스 /proc/stat, 전 코어 대비 %) · load1 ·
         프로세스 묶음별 CPU(top 단위, 100 = 코어 1개)와 RSS — ollama(--ollama-proc) · AI 서버(--ai-proc) ·
         모니터 자신(자식 ioreg·ps 포함). **서비스가 GPU 대신 CPU 를 잡아먹는지**를 같은 시간축에서 본다.
         kernel_cpu = 전체 − ps 로 보이는 프로세스 합(맥 kernel_task 는 일반 사용자 ps 에 안 보인다 — 메모리 압축·스왑·
         발열 제어가 여기로 잡힌다, 짧게 살다 간 프로세스 포함) · other_top = 서비스 밖 CPU 상위 3개.

경보(화면): 메모리 압박 ≥ 2 · CPU 밀림 > 0 · recoveryCount 증가 · 스왑 1초에 256MB 이상 증가 · 전체 CPU ≥ 90%.
종료: Ctrl+C · SIGINT · SIGTERM 모두 요약을 찍고 끝난다(백그라운드 실행은 SIGINT 무시로 시작되므로 직접 되살린다).
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "out"
FIELDS = ["ts", "elapsed_s", "gpu_util", "gpu_renderer", "gpu_alloc_mb", "gpu_inuse_mb", "gpu_recovery",
          "mem_pressure", "swap_used_mb", "pageouts_per_s", "ollama_models", "ollama_size_mb", "ollama_vram_mb", "ollama_cpu_pct",
          "cpu_total_pct", "load1", "ollama_proc_cpu", "ollama_proc_rss_mb", "ai_proc_cpu", "ai_proc_rss_mb", "self_cpu", "kernel_cpu", "other_top",
          "ai_running", "ai_queued", "ai_completed", "ai_failed", "tag_extract_total",
          "nv_util", "nv_mem_used_mb", "nv_mem_total_mb", "nv_temp_c", "nv_power_w", "alert"]
NCPU = os.cpu_count() or 1
AI_PROC = r"uvicorn|profiling\.main|scenario_callback30|extract_tags"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mac_gpu() -> dict:
    out = _run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
    m = re.search(r'"PerformanceStatistics" = \{([^}]*)\}', out)
    if not m:
        return {}
    kv = dict(re.findall(r'"([^"]+)"=(\d+)', m.group(1)))
    mb = lambda k: round(int(kv[k]) / 1048576) if k in kv else None
    return {"gpu_util": int(kv.get("Device Utilization %", 0)), "gpu_renderer": int(kv.get("Renderer Utilization %", 0)),
            "gpu_alloc_mb": mb("Alloc system memory"), "gpu_inuse_mb": mb("In use system memory"), "gpu_recovery": int(kv.get("recoveryCount", 0))}


def mac_memory() -> dict:
    p = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    sw = re.search(r"used = ([\d.]+)M", _run(["sysctl", "vm.swapusage"]))
    po = re.search(r"Pageouts:\s+(\d+)", _run(["vm_stat"]))
    return {"mem_pressure": int(p) if p.isdigit() else None, "swap_used_mb": round(float(sw.group(1))) if sw else None,
            "_pageouts": int(po.group(1)) if po else None}


def nvidia() -> dict:
    if not shutil.which("nvidia-smi"):
        return {}
    line = _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"]).splitlines()
    if not line:
        return {}
    u, mu, mt, t, pw = (x.strip() for x in line[0].split(","))
    return {"nv_util": u, "nv_mem_used_mb": mu, "nv_mem_total_mb": mt, "nv_temp_c": t, "nv_power_w": pw}


def ollama(url: str) -> dict:
    try:
        ms = httpx.get(f"{url}/api/ps", timeout=2).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return {}
    size = sum(m.get("size", 0) for m in ms)
    vram = sum(m.get("size_vram", 0) for m in ms)
    return {"ollama_models": "+".join(f"{m['name']}({m['details'].get('quantization_level', '')})" for m in ms),
            "ollama_size_mb": round(size / 1048576), "ollama_vram_mb": round(vram / 1048576),
            "ollama_cpu_pct": round(100 * (size - vram) / size, 1) if size else 0}


def ai_health(url: str | None) -> dict:
    if not url:
        return {}
    try:
        s = httpx.get(f"{url}/health", timeout=2).json().get("supervisor", {})
    except (httpx.HTTPError, ValueError):
        return {}
    return {"ai_running": s.get("running"), "ai_queued": s.get("queued"), "ai_completed": s.get("completed"), "ai_failed": s.get("failed")}


def cpu_ticks() -> tuple[int, int] | None:
    """(바쁜 틱, 전체 틱) 누적 — 두 번 읽은 차이로 전체 CPU % 를 낸다."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        libc.mach_host_self.restype = ctypes.c_uint
        info, cnt = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
        if libc.host_statistics(libc.mach_host_self(), 3, info, ctypes.byref(cnt)) != 0:   # HOST_CPU_LOAD_INFO
            return None
        user, system, idle, nice = info
        return user + system + nice, user + system + idle + nice
    stat = Path("/proc/stat")
    if stat.exists():
        v = [int(x) for x in stat.read_text().split("\n", 1)[0].split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v) - idle, sum(v)
    return None


def _cpu_s(t: str) -> float:
    """ps 의 time(누적 CPU) '[dd-][hh:]mm:ss.cc' → 초."""
    d, _, t = t.rpartition("-")
    s = 0.0
    for part in t.split(":"):
        s = s * 60 + float(part)
    return s + (int(d) * 86400 if d else 0)


def procs(groups: dict[str, re.Pattern]) -> tuple[dict[str, dict[int, tuple[float, int]]], dict[int, tuple[float, str]]]:
    """묶음별 {pid: (누적 CPU 초, RSS KB)} 와 묶음 밖 {pid: (누적 CPU 초, 짧은 이름)} — 모니터 자신은 뺀다."""
    found: dict[str, dict[int, tuple[float, int]]] = {g: {} for g in groups}
    other: dict[int, tuple[float, str]] = {}
    me = os.getpid()
    for line in _run(["ps", "-A", "-o", "pid=,time=,rss=,command="]).splitlines():
        f = line.split(None, 3)
        if len(f) < 4 or int(f[0]) == me:
            continue
        for g, rx in groups.items():
            if rx.search(f[3]):
                found[g][int(f[0])] = (_cpu_s(f[1]), int(f[2]))
                break
        else:
            other[int(f[0])] = (_cpu_s(f[1]), Path(f[3].split(" --", 1)[0]).name[:24])
    return found, other


def self_cpu_s() -> float:
    a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


class CpuSampler:
    """누적값을 기억했다가 샘플마다 차이로 % 를 낸다. 첫 샘플은 기준만 잡고 빈칸."""

    def __init__(self, ollama_proc: str, ai_proc: str) -> None:
        self.groups = {"ollama": re.compile(ollama_proc, re.IGNORECASE), "ai": re.compile(ai_proc)}
        self.prev_t = self.prev_ticks = self.prev_self = None
        self.prev_procs: dict[str, dict[int, tuple[float, int]]] = {}
        self.prev_other: dict[int, tuple[float, str]] = {}

    def sample(self) -> dict:
        now, ticks, me = time.time(), cpu_ticks(), self_cpu_s()
        ps, other = procs(self.groups)
        r: dict = {"load1": round(os.getloadavg()[0], 2)}
        for g, members in ps.items():
            r[f"{g}_proc_rss_mb"] = round(sum(rss for _, rss in members.values()) / 1024) if members else 0
        if self.prev_t is not None:
            dt = max(now - self.prev_t, 1e-3)
            if ticks and self.prev_ticks and ticks[1] > self.prev_ticks[1]:
                r["cpu_total_pct"] = round(100 * (ticks[0] - self.prev_ticks[0]) / (ticks[1] - self.prev_ticks[1]), 1)
            for g, members in ps.items():
                old = self.prev_procs.get(g, {})
                # 새로 생긴 pid 는 시작 뒤 쓴 CPU 전부가 이 구간 몫 · 사라진 pid 는 셈에서 빠진다
                used = sum(max(0.0, c - old.get(pid, (0.0, 0))[0]) for pid, (c, _) in members.items())
                r[f"{g}_proc_cpu"] = round(100 * used / dt, 1)
            r["self_cpu"] = round(100 * (me - self.prev_self) / dt, 1)
            use = sorted(((100 * max(0.0, c - self.prev_other.get(pid, (0.0, ""))[0]) / dt, name) for pid, (c, name) in other.items()),
                         reverse=True)
            r["other_top"] = " ".join(f"{name}:{u:.0f}" for u, name in use[:3] if u >= 1)
            if "cpu_total_pct" in r:
                seen = sum(u for u, _ in use) + r["self_cpu"] + sum(r[f"{g}_proc_cpu"] for g in ps)
                r["kernel_cpu"] = round(max(0.0, r["cpu_total_pct"] * NCPU - seen), 1)
        self.prev_t, self.prev_ticks, self.prev_self, self.prev_procs, self.prev_other = now, ticks, me, ps, other
        return r


def count_tag_extract(path: str | None) -> int | None:
    if not path or not Path(path).exists():
        return None
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if "tag_extract" in line)


def monitor(a: argparse.Namespace) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"gpu_{a.label + '_' if a.label else ''}{datetime.now().astimezone():%Y%m%d_%H%M%S}.csv"
    t0, prev_po, prev_swap, prev_rec, rows = time.time(), None, None, None, 0
    cpu = CpuSampler(a.ollama_proc, a.ai_proc)
    print(f"기록 시작 → {path}  (Ctrl+C 로 끝)", flush=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        try:
            while a.duration <= 0 or time.time() - t0 < a.duration:
                tick = time.time()
                r = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "elapsed_s": round(tick - t0, 1)}
                r |= mac_gpu() | mac_memory() | nvidia() | ollama(a.ollama) | ai_health(a.ai) | cpu.sample()
                r["tag_extract_total"] = count_tag_extract(a.log)
                po = r.pop("_pageouts", None)
                r["pageouts_per_s"] = round((po - prev_po) / max(a.interval, 0.1)) if po is not None and prev_po is not None else 0
                prev_po = po
                alerts = []
                if (r.get("mem_pressure") or 1) >= 2:
                    alerts.append(f"메모리압박{r['mem_pressure']}")
                if (r.get("ollama_cpu_pct") or 0) > 0:
                    alerts.append(f"CPU밀림{r['ollama_cpu_pct']}%")
                if prev_rec is not None and (r.get("gpu_recovery") or 0) > prev_rec:
                    alerts.append("GPU리셋")
                if prev_swap is not None and r.get("swap_used_mb") is not None and r["swap_used_mb"] - prev_swap >= 256:
                    alerts.append(f"스왑+{r['swap_used_mb'] - prev_swap}MB")
                if (r.get("cpu_total_pct") or 0) >= 90:
                    alerts.append(f"CPU포화{r['cpu_total_pct']}%")
                prev_rec, prev_swap = r.get("gpu_recovery"), r.get("swap_used_mb")
                r["alert"] = " · ".join(alerts)
                w.writerow({k: r.get(k) for k in FIELDS})
                f.flush()
                rows += 1
                if a.verbose or alerts:
                    print(f"{r['ts'][11:]} GPU {r.get('gpu_util')}% · GPU메모리 {r.get('gpu_alloc_mb')}MB · 압박 {r.get('mem_pressure')} · "
                          f"스왑 {r.get('swap_used_mb')}MB · Ollama {r.get('ollama_models') or '-'} CPU {r.get('ollama_cpu_pct')}% · "
                          f"CPU 전체 {r.get('cpu_total_pct')}% · ollama {r.get('ollama_proc_cpu')} · AI {r.get('ai_proc_cpu')} · 모니터 {r.get('self_cpu')} · "
                          f"커널 {r.get('kernel_cpu')} · 밖 {r.get('other_top') or '-'} · "
                          f"AI 실행 {r.get('ai_running')} 대기 {r.get('ai_queued')}" + (f"  ⚠ {r['alert']}" if alerts else ""), flush=True)
                time.sleep(max(0.0, a.interval - (time.time() - tick)))
        except KeyboardInterrupt:
            pass
    print(f"기록 끝 — {rows}행 → {path}")
    summarize(path)
    return path


def _num(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def summarize(path: Path) -> dict:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        return {}
    col = lambda k: [v for v in (_num(r.get(k)) for r in rows) if v is not None]
    q = lambda xs, p: sorted(xs)[round(p * (len(xs) - 1))] if xs else None
    s = {"rows": len(rows), "seconds": _num(rows[-1]["elapsed_s"]),
         "gpu_util_p50": q(col("gpu_util"), .5), "gpu_util_p95": q(col("gpu_util"), .95), "gpu_alloc_max_mb": max(col("gpu_alloc_mb"), default=None),
         "pressure_ge2_s": sum(1 for v in col("mem_pressure") if v >= 2), "pressure_max": max(col("mem_pressure"), default=None),
         "swap_start_mb": col("swap_used_mb")[0] if col("swap_used_mb") else None, "swap_max_mb": max(col("swap_used_mb"), default=None),
         "pageouts_per_s_max": max(col("pageouts_per_s"), default=None), "ollama_cpu_pct_max": max(col("ollama_cpu_pct"), default=None),
         "gpu_recovery_delta": (max(col("gpu_recovery")) - min(col("gpu_recovery"))) if col("gpu_recovery") else None,
         "ai_queued_max": max(col("ai_queued"), default=None), "alerts": sum(1 for r in rows if r.get("alert")),
         "ncpu": NCPU, "cpu_total_p50": q(col("cpu_total_pct"), .5), "cpu_total_p95": q(col("cpu_total_pct"), .95),
         "cpu_total_max": max(col("cpu_total_pct"), default=None), "load1_max": max(col("load1"), default=None),
         "ollama_cpu_p50": q(col("ollama_proc_cpu"), .5), "ollama_cpu_p95": q(col("ollama_proc_cpu"), .95),
         "ai_cpu_p50": q(col("ai_proc_cpu"), .5), "ai_cpu_p95": q(col("ai_proc_cpu"), .95),
         "ai_rss_max_mb": max(col("ai_proc_rss_mb"), default=None),
         "kernel_cpu_p50": q(col("kernel_cpu"), .5), "kernel_cpu_p95": q(col("kernel_cpu"), .95),
         "self_cpu_mean": round(sum(col("self_cpu")) / len(col("self_cpu")), 1) if col("self_cpu") else None}
    print("요약 " + json.dumps(s, ensure_ascii=False))
    path.with_suffix(".summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def plot(path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "Apple SD Gothic Neo", "axes.unicode_minus": False, "figure.dpi": 140})
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    t = [float(r["elapsed_s"]) for r in rows]
    g = lambda k: [_num(r.get(k)) for r in rows]
    fig, ax = plt.subplots(5, 1, figsize=(12, 11), sharex=True)
    ax[0].plot(t, g("gpu_util"), color="#007D77", lw=1); ax[0].set_ylabel("GPU %"); ax[0].set_ylim(0, 105)
    ax[1].plot(t, g("gpu_alloc_mb"), color="#007D77", lw=1, label="GPU 가 잡은 메모리")
    ax[1].plot(t, g("ollama_vram_mb"), color="#E8A33D", lw=1, label="Ollama 모델(VRAM)"); ax[1].set_ylabel("MB"); ax[1].legend(frameon=False, fontsize=8)
    ax[2].plot(t, g("swap_used_mb"), color="#C0392B", lw=1, label="스왑 사용"); ax[2].set_ylabel("스왑 MB")
    a2 = ax[2].twinx(); a2.step(t, g("mem_pressure"), color="#555", lw=1, where="post"); a2.set_ylabel("압박(1·2·4)"); a2.set_ylim(0, 5)
    ax[3].step(t, g("ai_queued"), color="#E8A33D", where="post", label="AI 대기열")
    ax[3].step(t, g("ai_running"), color="#007D77", where="post", label="AI 실행 중")
    ax[3].plot(t, g("ollama_cpu_pct"), color="#C0392B", lw=1, label="Ollama CPU 밀림 %"); ax[3].legend(frameon=False, fontsize=8)
    cores = lambda k, f: [v * f if v is not None else None for v in g(k)]
    ax[4].plot(t, cores("cpu_total_pct", NCPU / 100), color="#111", lw=1, label="전체")
    ax[4].plot(t, cores("ollama_proc_cpu", .01), color="#E8A33D", lw=1, label="Ollama")
    ax[4].plot(t, cores("ai_proc_cpu", .01), color="#007D77", lw=1, label="AI 서버")
    ax[4].plot(t, cores("kernel_cpu", .01), color="#C0392B", lw=1, label="커널·미집계")
    ax[4].plot(t, cores("self_cpu", .01), color="#999", lw=1, ls=":", label="모니터 자신")
    ax[4].axhline(NCPU, color="#C0392B", lw=.8, ls="--"); ax[4].set_ylim(0, NCPU * 1.05)
    ax[4].set_ylabel(f"CPU 코어(최대 {NCPU})"); ax[4].legend(frameon=False, fontsize=8, ncol=5); ax[4].set_xlabel("초")
    for a in ax:
        for r, x in zip(rows, t, strict=False):
            if r.get("alert"):
                a.axvline(x, color="#C0392B", alpha=.08, lw=2)
    fig.suptitle(f"GPU · 메모리 · 작업 · CPU — {path.stem} (붉은 띠 = 경보)")
    out = path.with_suffix(".png")
    fig.tight_layout(); fig.savefig(out, facecolor="white"); plt.close(fig)
    print("그림", out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=1.0, help="샘플 간격(초)")
    ap.add_argument("--duration", type=float, default=0, help="이 초만큼 기록하고 끝(0 = Ctrl+C 까지)")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--ai", default=None, help="AI 서버 주소 — /health 의 대기열 · 실행 중을 같이 기록")
    ap.add_argument("--log", default=None, help="tag_extract 줄을 셀 로그 파일(예 /tmp/be-console-ai.log)")
    ap.add_argument("--ollama-proc", default="ollama", help="Ollama 프로세스를 고르는 정규식(대소문자 무시)")
    ap.add_argument("--ai-proc", default=AI_PROC, help="AI 서버 프로세스를 고르는 정규식(명령줄 전체에 맞춤)")
    ap.add_argument("--label", default="", help="파일 이름 꼬리표(예 q6)")
    ap.add_argument("--verbose", action="store_true", help="경보가 없어도 매 샘플 찍기")
    ap.add_argument("--plot", type=Path, help="기존 CSV 로 그림 · 요약만 만든다")
    a = ap.parse_args()
    if a.plot:
        summarize(a.plot)
        plot(a.plot)
        return 0
    signal.signal(signal.SIGINT, signal.default_int_handler)   # 백그라운드(&)로 띄우면 SIGINT 가 무시된 채 시작된다
    signal.signal(signal.SIGTERM, _stop)
    monitor(a)
    return 0


def _stop(*_: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
