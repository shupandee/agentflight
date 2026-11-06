"""Observability must never become a dependency for successful agent execution.

Every test here breaks the recorder in some way and asserts the agent still returns its
normal result (or raises exactly its own exception), without being blocked.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
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
