"""Batch queue size and dropped-span accounting."""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

import agentflight
from agentflight import attributes as A
from agentflight import sdk
from agentflight.sdk import resolve_queue_size


def _batch_processor(rec: sdk.Recorder) -> BatchSpanProcessor:
    assert rec._queue is not None
    assert isinstance(rec._queue.inner, BatchSpanProcessor)
    return rec._queue.inner


def _queue_limit(proc: BatchSpanProcessor) -> int:
    inner = getattr(proc, "_batch_processor", proc)
    return int(getattr(inner, "_max_queue_size", getattr(inner, "max_queue_size", -1)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTFLIGHT_MAX_QUEUE_SIZE", "OTEL_BSP_MAX_QUEUE_SIZE"):
        monkeypatch.delenv(name, raising=False)


def test_default_queue_size_is_unchanged() -> None:
    assert resolve_queue_size() == 2048
    rec = agentflight.configure("http://127.0.0.1:9", batch=True)
    assert rec.max_queue_size == 2048
    assert _queue_limit(_batch_processor(rec)) == 2048
    agentflight.shutdown(timeout_millis=100)


def test_queue_size_from_configure_is_passed_to_the_batch_processor() -> None:
    rec = agentflight.configure("http://127.0.0.1:9", batch=True, max_queue_size=100)
    proc = _batch_processor(rec)
    assert _queue_limit(proc) == 100
    # A queue smaller than OpenTelemetry's export batch (512) still works.
    inner = getattr(proc, "_batch_processor", proc)
    assert int(getattr(inner, "_max_export_batch_size", 100)) <= 100
    agentflight.shutdown(timeout_millis=100)


def test_queue_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "300")
    assert resolve_queue_size() == 300
    monkeypatch.setenv("AGENTFLIGHT_MAX_QUEUE_SIZE", "5000")
    assert resolve_queue_size() == 5000  # ours wins over OpenTelemetry's
    rec = agentflight.configure("http://127.0.0.1:9", batch=True)
    assert _queue_limit(_batch_processor(rec)) == 5000
    agentflight.shutdown(timeout_millis=100)
    assert resolve_queue_size(10) == 10  # the argument wins over both


def test_bad_queue_sizes(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("AGENTFLIGHT_MAX_QUEUE_SIZE", "lots")
    with caplog.at_level("WARNING"):
        assert resolve_queue_size() == 2048
    assert "AGENTFLIGHT_MAX_QUEUE_SIZE" in caplog.text
    with pytest.raises(ValueError):
        resolve_queue_size(0)
    # configure() never raises: a bad size disables recording with a warning.
    rec = agentflight.configure("http://127.0.0.1:9", max_queue_size=0)
    with agentflight.run("still runs"):
        pass
    assert not rec.provider._active_span_processor._span_processors


class _Blocked(SpanExporter):
    """An exporter that never finishes until released, so the queue fills up."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.exported: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.release.wait(10)
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS


def _fill(queue: int, spans: int) -> tuple[sdk.Recorder, _Blocked, str]:
    exporter = _Blocked()
    rec = agentflight.configure(exporter=exporter, batch=True, max_queue_size=queue)
    with agentflight.run("burst") as r:
        for i in range(spans):
            agentflight.decision(f"d{i}", i)
    return rec, exporter, r.run_id


def _run_span(exporter: _Blocked, run_id: str) -> ReadableSpan:
    return next(
        s
        for s in exporter.exported
        if format(s.get_span_context().trace_id, "032x") == run_id
        and (s.attributes or {}).get(A.KIND) == A.RUN
    )


def test_dropped_spans_are_counted_and_recorded_on_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        rec, exporter, run_id = _fill(queue=80, spans=300)
    exporter.release.set()
    assert agentflight.flush(timeout_millis=5_000)
    # 80 slots (run spans don't use them): the first 80 decisions fit, and the export
    # worker may already have taken some of them off the queue, so at most 220 dropped.
    dropped = rec.dropped_spans
    assert 0 < dropped <= 300 - 80
    run_span = _run_span(exporter, run_id)  # the run span itself got through
    assert (run_span.attributes or {})[A.RUN_DROPPED_SPANS] == dropped
    assert len(exporter.exported) == 301 - dropped  # nothing lost without being counted
    warnings = [r for r in caplog.records if "export queue full" in r.getMessage()]
    assert len(warnings) == 1
    assert "Queue full" not in caplog.text  # OpenTelemetry's own queue never overflowed
    agentflight.shutdown()


def test_runs_without_drops_have_no_count() -> None:
    rec, exporter, run_id = _fill(queue=2048, spans=50)
    exporter.release.set()
    assert agentflight.flush(timeout_millis=5_000)
    assert rec.dropped_spans == 0
    assert A.RUN_DROPPED_SPANS not in (_run_span(exporter, run_id).attributes or {})
    agentflight.shutdown()


def test_drops_after_the_run_ended_are_counted_but_not_on_the_run() -> None:
    exporter = _Blocked()
    rec = agentflight.configure(exporter=exporter, batch=True, max_queue_size=16)
    with agentflight.run("short") as r:
        pass
    # Spans outside any run (or after it ended) fill the queue: counted in the total only.
    tracer = rec.tracer
    for i in range(40):
        tracer.start_span(f"late{i}").end()
    assert rec.dropped_spans > 0
    exporter.release.set()
    assert agentflight.flush(timeout_millis=5_000)
    assert A.RUN_DROPPED_SPANS not in (_run_span(exporter, r.run_id).attributes or {})
    agentflight.shutdown()


def test_batch_false_never_drops() -> None:
    rec = agentflight.configure(exporter=_Blocked(), batch=False)
    assert rec.dropped_spans == 0
    assert rec._queue is None
    agentflight.shutdown()
