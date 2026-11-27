"""Run spans skip the bounded batch queue, so runs with drops still close."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from sqlalchemy import Engine

import agentflight
from agentflight import _queue
from agentflight import attributes as A
from agentflight.exporter import AgentFlightExporter
from agentflight.server import create_app
from agentflight.server.settings import Settings
from tests.test_failure_isolation import _HangingServer


@pytest.fixture
def c(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)


@pytest.fixture
def hanging_server() -> Iterator[_HangingServer]:
    server = _HangingServer()
    yield server
    server.close()


def _is_run(span: ReadableSpan) -> bool:
    return (span.attributes or {}).get(A.KIND) == A.RUN


class _Counting(AgentFlightExporter):
    """The real exporter; counts every run span it is asked to send. The first batch from
    the queue is held until ``release`` is set, so the queue fills up."""

    def __init__(self, client: TestClient) -> None:
        super().__init__("http://testserver", client=client)
        self.release = threading.Event()
        self.run_spans: Counter[int] = Counter()

    def _count(self, spans: Sequence[ReadableSpan]) -> None:
        self.run_spans.update(s.get_span_context().span_id for s in spans if _is_run(s))

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.release.wait(10)
        self._count(spans)
        return super().export(spans)

    def export_within(self, spans: Sequence[ReadableSpan], deadline_s: float) -> SpanExportResult:
        self._count(spans)
        return super().export_within(spans, deadline_s)


# TestClient warns about per-request timeouts, which the run lane's deadline uses.
@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_tight_loop_runs_close_with_their_dropped_count(c: TestClient) -> None:
    exporter = _Counting(c)
    rec = agentflight.configure(exporter=exporter, batch=True, max_queue_size=32)
    run_ids = []
    for n in range(30):  # a tight loop: the queue is full after the first run
        with agentflight.run(f"burst{n}") as r:
            for i in range(60):
                agentflight.decision(f"d{i}", i)
        run_ids.append(r.run_id)
    exporter.release.set()
    assert agentflight.flush(timeout_millis=20_000)
    dropped = rec.dropped_spans
    agentflight.shutdown()

    assert dropped > 0
    runs = {r: c.get(f"/v1/runs/{r}").json() for r in run_ids}
    assert all(run["status"] == "ok" for run in runs.values())  # every run closed
    assert sum(run["dropped_spans"] for run in runs.values()) == dropped  # all on their runs
    for run in runs.values():
        assert run["event_count"] == 61 - run["dropped_spans"]  # nothing lost uncounted
    # Each run span went out exactly once, through the run lane, never the batch queue.
    assert len(exporter.run_spans) == 30
    assert set(exporter.run_spans.values()) == {1}
    worst = max(runs, key=lambda r: runs[r]["dropped_spans"])
    assert runs[worst]["dropped_spans"] > 0
    page = c.get(f"/ui/runs/{worst}").text
    assert f"incomplete recording: {runs[worst]['dropped_spans']:,} span" in page
    assert "incomplete recording" in c.get("/ui").text
    assert c.get("/v1/runs", params={"status": "running"}).json()["total"] == 0


class _Blocked(SpanExporter):
    def __init__(self) -> None:
        self.release = threading.Event()
        self.exported: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.release.wait(10)
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS


def test_ending_a_run_never_waits_for_the_network() -> None:
    exporter = _Blocked()  # every send hangs until released
    agentflight.configure(exporter=exporter, batch=True)
    start = time.perf_counter()
    for n in range(20):
        with agentflight.run(f"r{n}"):
            agentflight.decision("d", n)
    assert time.perf_counter() - start < 0.5
    exporter.release.set()
    assert agentflight.flush(timeout_millis=5_000)  # flush waits for the run lane too
    assert sum(_is_run(s) for s in exporter.exported) == 20
    agentflight.shutdown()


def test_run_span_send_has_a_deadline(hanging_server: _HangingServer) -> None:
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=5.0)
    rec = agentflight.configure(exporter=exporter, batch=True)
    assert rec._queue is not None
    rec._queue.run_span_deadline_s = 0.3
    start = time.perf_counter()
    with agentflight.run("hangs"):
        pass
    assert time.perf_counter() - start < 0.1  # the agent didn't wait
    assert rec._queue.runs.flush(3.0)  # the send gave up at its deadline, not the 5 s timeout
    assert time.perf_counter() - start < 2.0
    agentflight.shutdown(timeout_millis=1_000)


def test_export_within_caps_the_whole_export(hanging_server: _HangingServer) -> None:
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=5.0)
    spans = _finished_runs(1)
    start = time.perf_counter()
    assert exporter.export_within(spans, 0.3) == SpanExportResult.FAILURE
    assert time.perf_counter() - start < 1.5
    exporter.shutdown()
    agentflight.shutdown()


def _finished_runs(n: int) -> list[ReadableSpan]:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    agentflight.configure(exporter=memory, batch=False)
    for i in range(n):
        with agentflight.run(f"r{i}"):
            pass
    return list(memory.get_finished_spans())


def test_full_run_lane_drops_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_queue, "RUN_LANE_SIZE", 2)
    exporter = _Blocked()
    rec = agentflight.configure(exporter=exporter, batch=True)
    for n in range(8):
        with agentflight.run(f"r{n}"):
            pass
    assert rec.dropped_spans > 0
    exporter.release.set()
    assert agentflight.flush(timeout_millis=5_000)
    assert len(exporter.exported) + rec.dropped_spans == 8
    agentflight.shutdown()


def test_run_spans_keep_their_lane_after_a_fork_reset() -> None:
    exporter = _Blocked()
    exporter.release.set()
    rec = agentflight.configure(exporter=exporter, batch=True)
    assert rec._queue is not None
    _queue._reset_lanes_after_fork()  # what a forked child does: no inherited thread
    with agentflight.run("child"):
        pass
    assert agentflight.flush(timeout_millis=5_000)
    assert [s.name for s in exporter.exported if _is_run(s)] == ["run:child"]
    agentflight.shutdown()
