"""Batch queue size and dropped-span accounting."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import agentflight
from agentflight import sdk
from agentflight.sdk import resolve_queue_size


def _batch_processor(rec: sdk.Recorder) -> BatchSpanProcessor:
    procs = rec.provider._active_span_processor._span_processors
    found = [p for p in procs if isinstance(p, BatchSpanProcessor)]
    assert found, procs
    return found[0]


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
