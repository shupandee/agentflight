"""Admission control in front of OpenTelemetry's batch span processor.

OpenTelemetry drops spans silently-ish when its queue is full (and, in 1.45, drops the
*oldest* queued span, which can belong to another run). AgentFlight keeps its own count
of spans waiting for export and refuses new ones itself before that queue can overflow,
so every drop is known: it is counted per run (recorded on the run span when the run
ends) and in total.

Run spans don't use that queue at all. They go to a small lane of their own, sent by a
dedicated thread as soon as they end, so a run whose other spans were dropped still
closes on the server and carries its drop count.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from agentflight import attributes as A

logger = logging.getLogger("agentflight")

RUN_LANE_SIZE = 1024
"""Run spans waiting to be sent before new ones are dropped (each is one ended run)."""

RUN_LANE_BATCH = 512
"""Run spans sent in one request when several are waiting."""


class _Counted(SpanExporter):
    """Wraps the real exporter: a batch handed to it has left the queue."""

    def __init__(self, inner: SpanExporter, left_queue: Any, lock: threading.Lock) -> None:
        self._inner = inner
        self._left_queue = left_queue
        self._lock = lock

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._left_queue(len(spans))
        with self._lock:
            return self._inner.export(spans)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


class _RunLane:
    """A bounded lane for run spans with its own sender thread.

    ``submit`` only appends under a lock, so the agent thread never waits on the network
    here. The thread starts with the first run span (and again in a forked child).
    """

    def __init__(self, send: Callable[[list[ReadableSpan]], None], *, capacity: int) -> None:
        self._send = send
        self._capacity = capacity
        self._reset()
        _lanes.add(self)

    def _reset(self) -> None:
        self._items: deque[ReadableSpan] = deque()
        self._cond = threading.Condition()
        self._busy = False
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, span: ReadableSpan) -> bool:
        """Queue ``span`` for sending; False if the lane is full or shut down."""
        with self._cond:
            if self._closed or len(self._items) >= self._capacity:
                return False
            self._items.append(span)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._worker, name="agentflight-run-spans", daemon=True
                )
                self._thread.start()
            self._cond.notify()
        return True

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if not self._items:
                    return
                batch = [
                    self._items.popleft() for _ in range(min(RUN_LANE_BATCH, len(self._items)))
                ]
                self._busy = True
            try:
                self._send(batch)
            except Exception:
                logger.warning(
                    "agentflight: failed to send %d run spans", len(batch), exc_info=True
                )
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def flush(self, timeout_s: float) -> bool:
        """Wait at most ``timeout_s`` until every queued run span has been sent."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._items and not self._busy, timeout_s)

    def shutdown(self, timeout_s: float) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)


_lanes: weakref.WeakSet[_RunLane] = weakref.WeakSet()


def _reset_lanes_after_fork() -> None:
    for lane in list(_lanes):
        lane._reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lanes_after_fork)


class DropCountingBatchProcessor(SpanProcessor):
    """A ``BatchSpanProcessor`` that never overflows and counts what it refuses.

    Run spans skip the batch queue and go to their own lane (see the module docstring),
    so the span that carries a run's drop count isn't competing with the spans it counts.
    The wrapped exporter is only ever called by one thread at a time.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_queue_size: int,
        max_export_batch_size: int,
        run_span_deadline_s: float = 5.0,
    ) -> None:
        self.max_queue_size = max_queue_size
        self.run_span_deadline_s = run_span_deadline_s
        self.dropped_total = 0
        self._exporter = exporter
        self._export_lock = threading.Lock()
        self._pending = 0
        self._lock = threading.Lock()
        self._open_runs: dict[int, int] = {}  # trace id -> spans dropped so far
        self._warned = False
        self.inner = BatchSpanProcessor(
            _Counted(exporter, self._left_queue, self._export_lock),
            max_queue_size=max_queue_size,
            max_export_batch_size=max_export_batch_size,
        )
        self.runs = _RunLane(self._send_run_spans, capacity=RUN_LANE_SIZE)

    def _left_queue(self, n: int) -> None:
        with self._lock:
            self._pending = max(0, self._pending - n)

    def _send_run_spans(self, spans: list[ReadableSpan]) -> None:
        within = getattr(self._exporter, "export_within", None)
        with self._export_lock:
            if callable(within):
                within(spans, self.run_span_deadline_s)
            else:
                self._exporter.export(spans)

    def open_run(self, trace_id: int) -> None:
        with self._lock:
            self._open_runs.setdefault(trace_id, 0)

    def close_run(self, trace_id: int) -> int:
        """Stop counting for a run; returns the spans of it dropped so far."""
        with self._lock:
            return self._open_runs.pop(trace_id, 0)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self.inner.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if (span.attributes or {}).get(A.KIND) == A.RUN:
            if self.runs.submit(span):
                return
            self._dropped(span)  # the run lane is full: very many runs ending at once
            return
        with self._lock:
            admit = self._pending < self.max_queue_size
            if admit:
                self._pending += 1
        if admit:
            self.inner.on_end(span)
        else:
            self._dropped(span)

    def _dropped(self, span: ReadableSpan) -> None:
        with self._lock:
            self.dropped_total += 1
            ctx = span.get_span_context()
            trace_id = ctx.trace_id if ctx is not None else 0
            if trace_id in self._open_runs:
                self._open_runs[trace_id] += 1
            warn = not self._warned
            self._warned = True
        if warn:
            logger.warning(
                "agentflight: export queue full (%d spans); dropping spans. Runs record "
                "how many they lost; raise max_queue_size or check the server.",
                self.max_queue_size,
            )

    def shutdown(self) -> None:
        self.runs.shutdown(30.0)
        self.inner.shutdown()  # type: ignore[no-untyped-call]

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        runs_sent = self.runs.flush(timeout_millis / 1000)
        return self.inner.force_flush(timeout_millis) and runs_sent
