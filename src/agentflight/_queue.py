"""Admission control in front of OpenTelemetry's batch span processor.

OpenTelemetry drops spans silently-ish when its queue is full (and, in 1.45, drops the
*oldest* queued span, which can belong to another run). AgentFlight keeps its own count
of spans waiting for export and refuses new ones itself before that queue can overflow,
so every drop is known: it is counted per run (recorded on the run span when the run
ends) and in total.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from agentflight import attributes as A

logger = logging.getLogger("agentflight")


class _Counted(SpanExporter):
    """Wraps the real exporter: a batch handed to it has left the queue."""

    def __init__(self, inner: SpanExporter, left_queue: Any) -> None:
        self._inner = inner
        self._left_queue = left_queue

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._left_queue(len(spans))
        return self._inner.export(spans)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


class DropCountingBatchProcessor(SpanProcessor):
    """A ``BatchSpanProcessor`` that never overflows and counts what it refuses.

    A few slots are kept for run spans, so the span that carries a run's drop count is
    almost never the one dropped.
    """

    def __init__(
        self, exporter: SpanExporter, *, max_queue_size: int, max_export_batch_size: int
    ) -> None:
        self.max_queue_size = max_queue_size
        self.reserved_for_runs = min(64, max_queue_size // 8)
        self.dropped_total = 0
        self._pending = 0
        self._lock = threading.Lock()
        self._open_runs: dict[int, int] = {}  # trace id -> spans dropped so far
        self._warned = False
        self.inner = BatchSpanProcessor(
            _Counted(exporter, self._left_queue),
            max_queue_size=max_queue_size,
            max_export_batch_size=max_export_batch_size,
        )

    def _left_queue(self, n: int) -> None:
        with self._lock:
            self._pending = max(0, self._pending - n)

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
        is_run = (span.attributes or {}).get(A.KIND) == A.RUN
        limit = self.max_queue_size - (0 if is_run else self.reserved_for_runs)
        with self._lock:
            admit = self._pending < limit
            if admit:
                self._pending += 1
            else:
                self.dropped_total += 1
                ctx = span.get_span_context()
                trace_id = ctx.trace_id if ctx is not None else 0
                if trace_id in self._open_runs:
                    self._open_runs[trace_id] += 1
                warn = not self._warned
                self._warned = True
        if admit:
            self.inner.on_end(span)
        elif warn:
            logger.warning(
                "agentflight: export queue full (%d spans); dropping spans. Runs record "
                "how many they lost; raise max_queue_size or check the server.",
                self.max_queue_size,
            )

    def shutdown(self) -> None:
        self.inner.shutdown()  # type: ignore[no-untyped-call]

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)
