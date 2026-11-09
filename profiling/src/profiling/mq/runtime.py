"""MQ 부품 조립 — 서버(main.lifespan)가 켜고 끄는 한 덩어리 (체크리스트 8 · 설계 §5 · §7).

  RequestConsumer(받기 · ACK · 슬롯 · 멈춤 · 종료)  ─handler→  Processor(잠금 · 판정 · 분석 · 결과/실패 큐 발행)
                                                               └─ Publisher(발행 전용 스레드 · Confirm · Return)

- 켜기 조건: PROFILING_MQ_ENABLED=true. 설정이 빠졌거나 0 · 음수 · 모순이면 mq.config.MqConfigError → 서버가 뜨지 않는다.
- 소비 시작 조건(설계 §5 1): 연결 · 카탈로그 · DB 준비 — 서버의 /readyz 판정(ready)을 MQ_WAIT_S 간격으로 다시 묻는다
  (소비자는 0.2초마다 묻기 때문에 매번 DB 를 부르지 않게 결과를 그 간격 동안 둔다).
- 분석은 기존 pipeline.profile 을 그대로 부른다 — app.state 의 지금 부품(카탈로그 · 저장소 · 태그 추출기 · 임베딩)을 매번 읽는다.
- 대조 복구(CO-06): MQ_RECONCILE_INTERVAL_S 마다 장기 미완료 기록에서 빠진 단계만 마저 한다(mq.reconcile) — 별도 스레드 하나.
- 종료: 대조 복구 → 소비자(새 소비 중단 → 진행 중 대기 → 연결 닫기) → 발행기 순서.
- 통합 런타임(integration.profiler_runtime)은 main.lifespan 뒤에 부품(저장소 · 카탈로그 · 입력 기록)을 감싼다. 그래서
  · 저장소(profile_runs) · 분석 부품은 app.state 에서 **부를 때마다** 읽는다(감싼 것을 쓰게)
  · app.state.mq_defer_start 가 참이면 main.lifespan 이 시작하지 않는다 — 통합 런타임이 감싸기를 끝낸 뒤 start() 한다
  · app.state.mq_record_input(rq, input_hash) · app.state.mq_admit() 가 있으면 처리기가 부른다(process 머리말)
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from profiling import pipeline
from profiling.mq import config as mqc
from profiling.mq.consumer import RequestConsumer
from profiling.mq.metrics import MqStats
from profiling.mq.process import Processor
from profiling.mq.publisher import Publisher
from profiling.mq.reconcile import Reconciler
from profiling.mq.store import DbMqDeliveryStore
from profiling.settings import Settings
from profiling.types import ProfileOutcome, ProfileRequest

log = logging.getLogger("profiling.mq.runtime")


def db_connections(settings: Settings) -> int:
    """MQ 가 더 쓰는 DB 커넥션 수 — 슬롯마다 requestId 잠금 1 + (수신자, 버전) 잠금 1 을 분석 내내 쥔다. 대조 복구 스레드가 잠금 1.
    잠금 안의 저장 문장은 (수신자, 버전) 잠금 커넥션을 재사용하고, 잠금 밖 기록(접수 · 발행 확인 · SETTLED)은 잠깐 빌렸다 돌려준다."""
    if not settings.MQ_ENABLED or not settings.MQ_SLOTS:
        return 0
    return 2 * settings.MQ_SLOTS + 1


class _Cached:
    """ready() 결과를 ttl_s 동안 둔다(스레드 안전)."""

    def __init__(self, fn: Callable[[], bool], ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.fn, self.ttl_s, self.clock = fn, ttl_s, clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value = False

    def __call__(self) -> bool:
        with self._lock:
            now = self.clock()
            if self._at is None or now - self._at >= self.ttl_s:
                self._value, self._at = bool(self.fn()), now
            return self._value


class MqRuntime:
    def __init__(self, cfg: mqc.MqConfig, *, deliveries: Any, runs: Any, analyze: Callable[[ProfileRequest], ProfileOutcome],
                 ready: Callable[[], bool], publisher: Any = None, connect: Callable[[], object] | None = None,
                 record_input: Callable[[ProfileRequest, str], None] | None = None, admit: Callable[[], Any] | None = None) -> None:
        self.cfg = cfg
        self.deliveries = deliveries
        self.stats = MqStats()
        self.publisher = publisher or Publisher.from_url(cfg.url, confirm_timeout_s=cfg.confirm_timeout_s,
                                                        reconnect_delay_s=cfg.reconnect_delay_s)
        self.consumer = RequestConsumer(cfg, self._handle, ready=_Cached(ready, cfg.wait_s), connect=connect)
        self.processor = Processor(deliveries=deliveries, runs=runs, analyze=analyze, publisher=self.publisher, control=self.consumer,
                                   max_attempts=cfg.max_analysis_attempts, wait_s=cfg.wait_s, retry_base_s=cfg.retry_base_s,
                                   retry_max_s=cfg.retry_max_s, stats=self.stats, record_input=record_input, admit=admit)
        self.reconciler = Reconciler(deliveries, self.publisher, older_than_s=cfg.reconcile_after_s, stats=self.stats)
        self._stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self.started = False

    def _handle(self, d) -> None:
        self.processor.handle(d)

    def start(self) -> None:
        self.started = True
        self.publisher.start()
        self.consumer.start()
        self._reconcile_thread = threading.Thread(target=self._reconcile_loop, name="mq-reconcile", daemon=True)
        self._reconcile_thread.start()
        log.info("mq 소비자 시작 prefetch=%d slots=%d max_attempts=%d", self.cfg.prefetch, self.cfg.slots, self.cfg.max_analysis_attempts)

    def _reconcile_loop(self) -> None:
        while not self._stop.wait(self.cfg.reconcile_interval_s):
            try:
                self.reconciler.run_once()
            except Exception as e:  # noqa: BLE001 — DB 장애 등. 다음 차례에 다시(스레드가 죽으면 복구가 조용히 멈춘다)
                log.error("mq 대조 복구 실패: %s — %.0f초 뒤 다시", type(e).__name__, self.cfg.reconcile_interval_s)

    def stop(self) -> None:
        if not self.started:        # 시작 전에 끝남(통합 런타임 기동 실패 등) — 연결도 스레드도 없다
            return
        self._stop.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(self.cfg.confirm_timeout_s + 5)
        self.consumer.stop()
        self.publisher.stop()
        log.info("mq 소비자 종료 %s", self.stats.snapshot())

    def health(self) -> dict:
        """/health 의 mq 칸 — 소비자 상태 · 처리 숫자(이 프로세스) · 전달 기록(DB). requestId · 본문은 넣지 않는다."""
        out: dict[str, Any] = {"enabled": True, "consumer": self.consumer.status(), "counters": self.stats.snapshot(),
                               "reconcile": {"after_s": self.cfg.reconcile_after_s, "last": self.reconciler.last}}
        try:
            out["deliveries"] = self.deliveries.health_counts()
        except Exception as e:  # noqa: BLE001 — /health 는 DB 가 죽어도 200 이어야 한다(종류만 남김)
            out["deliveries"] = {"error": type(e).__name__}
        return out


def build(state: Any, settings: Settings, *, ready: Callable[[], bool]) -> MqRuntime:
    """app.state 의 부품으로 MQ 를 조립한다. 설정 문제는 MqConfigError(서버 기동 거부)."""
    cfg = mqc.load(settings)

    def analyze(rq: ProfileRequest) -> ProfileOutcome:
        return pipeline.profile(rq, catalog=state.catalog, store=state.store, recipient_store=state.recipient_store,
                                pool_size=settings.POOL_SIZE, dislike_mode=pipeline.DislikeMode(settings.DISLIKE_MODE),
                                pool_strategy=pipeline.PoolStrategy(settings.POOL_STRATEGY), tagger=getattr(state, "tagger", None),
                                embedding=getattr(state, "embedding", None), tag_spec=getattr(state, "tag_spec", None),
                                incremental_tags=settings.INCREMENTAL_TAGS)

    def record_input(rq: ProfileRequest, input_hash: str) -> None:
        fn = getattr(state, "mq_record_input", None)
        if fn is not None:
            fn(rq, input_hash)

    def admit():
        fn = getattr(state, "mq_admit", None)
        return fn() if fn is not None else nullcontext()

    return MqRuntime(cfg, deliveries=DbMqDeliveryStore(state.engine), runs=_StateStore(state), analyze=analyze, ready=ready,
                     record_input=record_input, admit=admit)


class _StateStore:
    """app.state.store 를 부를 때마다 읽는 대리 — 통합 런타임이 나중에 감싼 저장소(RequiredStore)를 쓰게."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state.store, name)
