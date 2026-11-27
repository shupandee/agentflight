"""Observability must never become a dependency for successful agent execution.

Every test here breaks the recorder in some way and asserts the agent still returns its
normal result (or raises exactly its own exception), without being blocked.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import agentflight
from agentflight import sdk
from agentflight.exporter import AgentFlightExporter


class AgentBug(Exception):
    """An exception raised by the agent itself, which must propagate unchanged."""


@agentflight.tool
def add(a: int, b: int) -> int:
    return a + b


@agentflight.tool
def fails() -> None:
    raise AgentBug("agent's own failure")


def toy_agent() -> int:
    """Exercises every recording API and returns a known value."""
    with agentflight.run("isolation", metadata={"k": "v"}):
        with agentflight.llm_call("m", [{"role": "user", "content": "hi"}]) as call:
            call.record("ok", tokens_in=1, tokens_out=1, cost_usd=0.1)
        agentflight.decision("d", {"x": 1}, reason="r")
        agentflight.state_change("s", [1], old=[])
        try:
            fails()
        except AgentBug as exc:
            agentflight.error(exc)
        return add(2, 3)


def assert_agent_unaffected() -> None:
    assert toy_agent() == 5
    with pytest.raises(AgentBug, match="agent's own failure"), agentflight.run("raises"):
        fails()
    agentflight.flush(timeout_millis=2_000)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    agentflight.shutdown()
    agentflight.configure(exporter=_NullExporter(), batch=False)


class _NullExporter(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        return SpanExportResult.SUCCESS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.parametrize("batch", [True, False])
def test_server_down(batch: bool) -> None:
    agentflight.configure(f"http://127.0.0.1:{_free_port()}", batch=batch)
    assert_agent_unaffected()


@pytest.mark.parametrize("status", [400, 404, 500, 503])
@pytest.mark.parametrize("batch", [True, False])
def test_server_error_status(status: int, batch: bool) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text="boom"))
    exporter = AgentFlightExporter(client=httpx.Client(transport=transport))
    agentflight.configure(exporter=exporter, batch=batch)
    assert_agent_unaffected()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"\x00\xff not json"),
        httpx.Response(202, json={"unexpected": ["shape"]}),
        httpx.Response(302, headers={"location": "http://elsewhere.invalid/"}),
        httpx.Response(200, headers={"content-length": "999"}, content=b"short"),
    ],
)
def test_malformed_responses(response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda r: response)
    exporter = AgentFlightExporter(client=httpx.Client(transport=transport))
    agentflight.configure(exporter=exporter, batch=False)
    assert_agent_unaffected()


@pytest.mark.parametrize(
    "exc", [RuntimeError("transport bug"), ValueError("x"), httpx.ReadTimeout("slow")]
)
def test_transport_raises(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    agentflight.configure(exporter=exporter, batch=False)
    assert_agent_unaffected()


class _HangingServer:
    """Accepts TCP connections and never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)

    def close(self) -> None:
        for c in self.conns:
            c.close()
        self.sock.close()


@pytest.fixture
def hanging_server() -> Iterator[_HangingServer]:
    server = _HangingServer()
    yield server
    server.close()


def test_slow_server_does_not_block_agent_in_batch_mode(hanging_server: _HangingServer) -> None:
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=5.0)
    agentflight.configure(exporter=exporter, batch=True)
    start = time.perf_counter()
    assert toy_agent() == 5
    assert time.perf_counter() - start < 0.5  # export happens on a background thread


def test_slow_server_is_bounded_by_timeout(hanging_server: _HangingServer) -> None:
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=0.2)
    agentflight.configure(exporter=exporter, batch=False)
    start = time.perf_counter()
    with agentflight.run("slow"):
        pass
    assert time.perf_counter() - start < 2.0


def test_flush_and_shutdown_are_bounded(hanging_server: _HangingServer) -> None:
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=0.3)
    agentflight.configure(exporter=exporter, batch=True)
    assert toy_agent() == 5
    start = time.perf_counter()
    agentflight.flush(timeout_millis=1_000)
    agentflight.shutdown()
    assert time.perf_counter() - start < 3.0


class _ExplodingExporter(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        raise RuntimeError("exporter bug")

    def shutdown(self) -> None:
        raise RuntimeError("shutdown bug")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        raise RuntimeError("flush bug")


@pytest.mark.parametrize("batch", [True, False])
def test_exporter_exceptions(batch: bool) -> None:
    agentflight.configure(exporter=_ExplodingExporter(), batch=batch)
    assert_agent_unaffected()
    agentflight.shutdown()


class _Evil:
    def __repr__(self) -> str:
        raise RuntimeError("repr bug")

    def __str__(self) -> str:
        raise RuntimeError("str bug")


class _Huge:
    def __repr__(self) -> str:
        return "x" * 1_000_000


def _cyclic() -> dict[str, Any]:
    d: dict[str, Any] = {}
    d["self"] = d
    return d


WEIRD = [
    _Evil(),
    _Huge(),
    _cyclic(),
    {1: object(), (1, 2): b"\xff"},
    float("nan"),
    {"gen": (i for i in range(3))},
    threading.Lock(),
]


@pytest.mark.parametrize("payload", WEIRD, ids=lambda p: type(p).__name__)
def test_weird_payloads(payload: Any) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)

    @agentflight.tool
    def echo(x: Any) -> Any:
        return x

    with agentflight.run("weird", metadata={"p": payload}):
        with agentflight.llm_call("m", payload, params={"p": payload}) as call:
            call.record(payload, tokens_in=1, tokens_out=1)
        assert echo(payload) is payload
        agentflight.decision("d", payload, reason="r")
        agentflight.state_change("s", payload, old=payload)


def test_error_with_broken_str_is_recorded_safely() -> None:
    class BadExc(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str bug")

    agentflight.configure(exporter=_NullExporter(), batch=False)

    @agentflight.tool
    def raises_bad() -> None:
        raise BadExc()

    with agentflight.run("r"):
        agentflight.error(BadExc())
        with pytest.raises(BadExc):
            raises_bad()


class _ExplodingProcessor(SpanProcessor):
    def __init__(self, on: str) -> None:
        self.on = on

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        if self.on == "start":
            raise RuntimeError("on_start bug")

    def on_end(self, span: ReadableSpan) -> None:
        if self.on == "end":
            raise RuntimeError("on_end bug")


@pytest.mark.parametrize("on", ["start", "end"])
def test_exceptions_in_span_processor_hooks(on: str) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)
    agentflight.get_recorder().provider.add_span_processor(_ExplodingProcessor(on))
    assert_agent_unaffected()


def test_tracer_itself_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)
    tracer = agentflight.get_recorder().tracer

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("tracer bug")

    monkeypatch.setattr(tracer, "start_as_current_span", broken)
    monkeypatch.setattr(tracer, "start_span", broken)
    assert_agent_unaffected()


def test_serializer_itself_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)

    def broken(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("dump bug")

    monkeypatch.setattr(sdk, "_dump", broken)
    assert_agent_unaffected()


def test_async_tool_with_broken_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)
    monkeypatch.setattr(
        agentflight.get_recorder().tracer,
        "start_as_current_span",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tracer bug")),
    )

    @agentflight.tool
    async def fetch(q: str) -> str:
        await asyncio.sleep(0)
        return q.upper()

    assert asyncio.run(fetch("x")) == "X"


def test_agent_exception_identity_preserved_when_recording_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentflight.configure(exporter=_NullExporter(), batch=False)
    monkeypatch.setattr(sdk, "_record_error", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    original = AgentBug("mine")
    with pytest.raises(AgentBug) as info, agentflight.run("r"):
        raise original
    assert info.value is original


def test_configure_with_bad_endpoint_does_not_raise() -> None:
    agentflight.configure("not a url at all", batch=False)
    assert_agent_unaffected()


def test_interpreter_exit_is_clean_when_exporter_breaks(tmp_path: Any) -> None:
    import subprocess
    import sys

    script = tmp_path / "agent.py"
    script.write_text(
        "import agentflight\n"
        "from opentelemetry.sdk.trace.export import SpanExporter\n"
        "class Boom(SpanExporter):\n"
        "    def export(self, spans): raise RuntimeError('export bug')\n"
        "    def shutdown(self): raise RuntimeError('shutdown bug')\n"
        "agentflight.configure(exporter=Boom())\n"
        "with agentflight.run('r'):\n"
        "    pass\n"
        "print('agent done')\n"
    )
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "agent done" in proc.stdout
    assert "Exception ignored" not in proc.stderr


def _finished_spans(n: int) -> list[ReadableSpan]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("test")
    for i in range(n):
        with tracer.start_as_current_span(f"s{i}"):
            pass
    return list(memory.get_finished_spans())


def test_batch_over_the_body_cap_is_split_and_sent() -> None:
    import json

    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        spans = json.loads(request.content)["spans"]
        if len(spans) > 2:  # pretend the server's cap fits two spans
            return httpx.Response(413)
        received.extend(s["name"] for s in spans)
        return httpx.Response(200, json={"accepted": len(spans)})

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert exporter.export(_finished_spans(7)) is SpanExportResult.SUCCESS
    assert sorted(received) == sorted(f"s{i}" for i in range(7))


def test_single_span_over_the_body_cap_fails_without_looping() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert exporter.export(_finished_spans(4)) is SpanExportResult.FAILURE
    assert calls == 7  # 4 spans -> 2+2 -> 1+1+1+1: every split tried once


def _export_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "agentflight.exporter" and r.levelname == "WARNING"
    ]


def test_single_oversized_span_is_dropped_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level("WARNING"):
        assert exporter.export(_finished_spans(1)) is SpanExportResult.FAILURE
    assert calls == 1
    warnings = _export_warnings(caplog)
    assert len(warnings) == 1
    assert "1 of 1 spans" in warnings[0] and "body cap" in warnings[0]


def test_every_dropped_span_in_one_export_shares_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = AgentFlightExporter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(413)))
    )
    with caplog.at_level("WARNING"):
        assert exporter.export(_finished_spans(4)) is SpanExportResult.FAILURE
    assert _export_warnings(caplog) == [
        "agentflight: failed to export 4 of 4 spans: "
        "span is larger than the server's body cap (HTTP 413)"
    ]


def test_one_half_failing_fails_the_export(caplog: pytest.LogCaptureFixture) -> None:
    # The second half fails after the first was stored: the export is a FAILURE.
    import json

    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        names = [s["name"] for s in json.loads(request.content)["spans"]]
        if len(names) > 2:
            return httpx.Response(413)
        if "s2" in names:
            return httpx.Response(500)
        received.extend(names)
        return httpx.Response(202)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level("WARNING"):
        assert exporter.export(_finished_spans(4)) is SpanExportResult.FAILURE
    assert sorted(received) == ["s0", "s1"]
    (warning,) = _export_warnings(caplog)
    assert "2 of 4 spans" in warning and "500" in warning


def _split_then(failure: Callable[[httpx.Request], httpx.Response]) -> tuple[list[int], Any]:
    """A server that answers 413 to batches over 2 spans and ``failure`` to the rest."""
    import json

    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["spans"])
        sizes.append(n)
        return httpx.Response(413) if n > 2 else failure(request)

    return sizes, handler


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_timeout, id="timeout"),
        pytest.param(_refused, id="transport-error"),
        pytest.param(lambda r: httpx.Response(503), id="5xx"),
        pytest.param(lambda r: httpx.Response(500), id="500"),
    ],
)
def test_first_non_413_failure_aborts_the_remaining_halves(
    failure: Callable[[httpx.Request], httpx.Response], caplog: pytest.LogCaptureFixture
) -> None:
    sizes, handler = _split_then(failure)
    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level("WARNING"):
        assert exporter.export(_finished_spans(8)) is SpanExportResult.FAILURE
    # 8 -> 413, 4 -> 413, 2 -> fails: the other 2 and the other 4 are never sent.
    assert sizes == [8, 4, 2]
    (warning,) = _export_warnings(caplog)
    assert "8 of 8 spans" in warning


def test_abort_does_not_stop_the_next_export() -> None:
    sizes, handler = _split_then(lambda r: httpx.Response(503))
    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    exporter.export(_finished_spans(8))
    exporter.export(_finished_spans(8))
    assert sizes == [8, 4, 2, 8, 4, 2]


def test_oversized_single_spans_do_not_abort_the_export() -> None:
    # A 413 on one span is that span's problem; its siblings are still sent.
    import json

    stored: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        names = [s["name"] for s in json.loads(request.content)["spans"]]
        if "s0" in names or "s5" in names:
            return httpx.Response(413)
        stored.extend(names)
        return httpx.Response(202)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert exporter.export(_finished_spans(8)) is SpanExportResult.FAILURE
    assert sorted(stored) == ["s1", "s2", "s3", "s4", "s6", "s7"]


def test_export_attempts_are_capped(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413)

    exporter = AgentFlightExporter(
        client=httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=5
    )
    with caplog.at_level("WARNING"):
        # Every span is oversized: uncapped, 64 spans would take 127 requests.
        assert exporter.export(_finished_spans(64)) is SpanExportResult.FAILURE
    assert calls == 5
    (warning,) = _export_warnings(caplog)
    assert "64 of 64 spans" in warning
    # The cap is per export: the next export gets a fresh budget.
    calls = 0
    exporter.export(_finished_spans(64))
    assert calls == 5


def test_default_cap_isolates_one_oversized_span_in_a_full_batch() -> None:
    import json

    received = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received, calls
        calls += 1
        names = [s["name"] for s in json.loads(request.content)["spans"]]
        if "s100" in names:  # one span too big on its own; everything containing it fails
            return httpx.Response(413)
        received += len(names)
        return httpx.Response(202)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert exporter.export(_finished_spans(512)) is SpanExportResult.FAILURE
    assert (received, calls) == (511, 19)  # 1 + 2 * log2(512), under the default of 32


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AgentFlightExporter(max_attempts=0)


def test_configure_passes_max_export_attempts() -> None:
    rec = agentflight.configure("http://x", batch=False, max_export_attempts=3)
    exporter = rec.provider._active_span_processor._span_processors[0].span_exporter
    assert exporter._max_attempts == 3
    agentflight.shutdown()


def test_flush_and_shutdown_are_bounded_with_a_full_queue(
    hanging_server: _HangingServer,
) -> None:
    # Four 512-span batches queued against a server that never answers: OpenTelemetry's
    # own force_flush would wait for every batch (4 x 1 s here); ours stops at its timeout.
    exporter = AgentFlightExporter(f"http://127.0.0.1:{hanging_server.port}", timeout=1.0)
    agentflight.configure(exporter=exporter, batch=True)
    with agentflight.run("many"):
        for i in range(2_000):
            agentflight.decision(f"d{i}", i)
    start = time.perf_counter()
    assert agentflight.flush(timeout_millis=300) is False
    assert time.perf_counter() - start < 0.8
    start = time.perf_counter()
    agentflight.shutdown(timeout_millis=300)
    assert time.perf_counter() - start < 0.8


def test_flush_reports_success_when_everything_is_sent() -> None:
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(202)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    agentflight.configure(exporter=exporter, batch=True)
    with agentflight.run("r"):
        pass
    assert agentflight.flush(timeout_millis=5_000) is True
    assert sent
    agentflight.shutdown()
