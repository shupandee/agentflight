"""The bounded on-disk spool: failed exports are kept and retried on the next export."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine

import agentflight
from agentflight import _spool
from agentflight._spool import SUFFIX, Spool
from agentflight.exporter import AgentFlightExporter, span_to_dict
from agentflight.server import create_app
from agentflight.server.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


class Server:
    """A fake ingest endpoint: answers ``status`` (or 413 above ``max_spans``)."""

    def __init__(self) -> None:
        self.status = 202
        self.max_spans: int | None = None
        self.received: list[list[str]] = []  # span names per accepted request
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        spans = json.loads(request.content)["spans"]
        if self.max_spans is not None and len(spans) > self.max_spans:
            return httpx.Response(413)
        if self.status == 202:
            self.received.append([s["name"] for s in spans])
        return httpx.Response(self.status)

    def exporter(self, spool: Spool | None, **kwargs: Any) -> AgentFlightExporter:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return AgentFlightExporter(client=client, spool=spool, **kwargs)

    @property
    def names(self) -> list[str]:
        return [n for batch in self.received for n in batch]


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(tmp_path / "spool")


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("AGENTFLIGHT_SPOOL_DIR", raising=False)
    yield
    agentflight.shutdown()


def spans(*names: str) -> list[ReadableSpan]:
    memory = InMemorySpanExporter()
    rec = agentflight.Recorder(memory, batch=False)
    for name in names:
        rec.tracer.start_span(name).end()
    return list(memory.get_finished_spans())


def spool_files(s: Spool) -> list[Path]:
    return sorted(p for p in s.directory.iterdir() if p.name.endswith(SUFFIX))


def test_failed_export_is_spooled_and_sent_on_the_next_export(server: Server, spool: Spool) -> None:
    exporter = server.exporter(spool)
    server.status = 503
    assert exporter.export(spans("a", "b")) is SpanExportResult.FAILURE
    assert spool.pending_spans() == 2
    server.status = 202
    assert exporter.export(spans("c")) is SpanExportResult.SUCCESS
    assert server.names == ["a", "b", "c"]  # the spooled batch went first
    assert spool.pending_spans() == 0
    assert spool.dropped == 0


def test_spooled_batches_are_replayed_oldest_first(server: Server, spool: Spool) -> None:
    exporter = server.exporter(spool)
    server.status = 503
    for name in ("first", "second", "third"):
        exporter.export(spans(name))
    assert len(spool_files(spool)) == 3
    server.status = 202
    exporter.export(spans("new"))
    assert server.received == [["first"], ["second"], ["third"], ["new"]]


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429, 401, 403])
def test_failures_that_may_clear_up_are_spooled(server: Server, spool: Spool, status: int) -> None:
    server.status = status
    server.exporter(spool).export(spans("a"))
    assert spool.pending_spans() == 1


def test_transport_errors_and_timeouts_are_spooled(spool: Spool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    exporter = AgentFlightExporter(
        client=httpx.Client(transport=httpx.MockTransport(handler)), spool=spool
    )
    assert exporter.export(spans("a", "b")) is SpanExportResult.FAILURE
    assert spool.pending_spans() == 2


@pytest.mark.parametrize("status", [400, 404, 422])
def test_permanent_rejections_are_not_spooled(server: Server, spool: Spool, status: int) -> None:
    server.status = status
    server.exporter(spool).export(spans("a"))
    assert spool.pending_spans() == 0


def test_a_single_span_over_the_body_cap_is_not_spooled(server: Server, spool: Spool) -> None:
    server.max_spans = 0  # every request is "too large"
    server.exporter(spool).export(spans("huge"))
    assert spool.pending_spans() == 0


def test_replay_respects_the_attempt_cap(server: Server, spool: Spool) -> None:
    exporter = server.exporter(spool, max_attempts=2)
    server.status = 503
    for name in ("s1", "s2", "s3"):
        exporter.export(spans(name))
    server.status = 202
    exporter.export(spans("new1"))  # 2 requests: s1, s2; then the cap is reached
    assert server.received == [["s1"], ["s2"]]
    assert server.requests == 3 + 2
    assert spool.pending_spans() == 2  # s3 and new1 wait on disk, nothing is lost
    exporter.export(spans("new2"))
    exporter.export(spans("new3"))
    assert server.names == ["s1", "s2", "s3", "new1", "new2", "new3"]
    assert spool.pending_spans() == 0
    assert spool.dropped == 0


def test_replayed_batches_are_split_on_413(server: Server, spool: Spool) -> None:
    exporter = server.exporter(spool)
    server.status = 503
    exporter.export(spans("a", "b", "c", "d"))
    server.status = 202
    server.max_spans = 2
    assert exporter.export([]) is SpanExportResult.SUCCESS
    assert server.received == [["a", "b"], ["c", "d"], []]
    assert spool.pending_spans() == 0


def test_a_partly_sent_file_keeps_only_the_unsent_spans(server: Server, spool: Spool) -> None:
    exporter = server.exporter(spool)
    server.status = 503
    exporter.export(spans("a", "b", "c", "d"))
    (before,) = spool_files(spool)
    born = before.name.split("-")[0]

    calls = 0
    original = server.handler

    def first_half_only(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(413)  # split ...
        if calls == 2:
            server.status = 202
            return original(request)  # ... first half stored ...
        server.status = 503
        return original(request)  # ... then the server goes down again

    exporter._client = httpx.Client(transport=httpx.MockTransport(first_half_only))
    exporter.export([])
    assert server.names == ["a", "b"]
    (after,) = spool_files(spool)
    assert after.name.split("-")[0] == born  # still first in line
    assert [s["name"] for s in json.loads(after.read_bytes())["spans"]] == ["c", "d"]
    assert spool.dropped == 0


def test_directory_and_files_are_private(tmp_path: Path, server: Server) -> None:
    old = os.umask(0)
    try:
        s = Spool(tmp_path / "a" / "spool")
        server.status = 503
        server.exporter(s).export(spans("a"))
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(s.directory).st_mode) == 0o700
    (f,) = spool_files(s)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_an_existing_open_directory_is_tightened(tmp_path: Path) -> None:
    d = tmp_path / "spool"
    d.mkdir()
    d.chmod(0o755)
    assert Spool(d).put([{"name": "a"}])
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_a_symlinked_directory_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    s = Spool(tmp_path / "link")
    with caplog.at_level("WARNING"):
        assert not s.put([{"name": "a"}])
    assert "not a directory" in caplog.text
    assert list(real.iterdir()) == []
    assert s.dropped == 1


def test_size_cap_deletes_the_oldest_files(tmp_path: Path, server: Server) -> None:
    one = len(json.dumps({"v": 1, "spans": [span_to_dict(spans("x")[0])]}, separators=(",", ":")))
    s = Spool(tmp_path / "spool", max_bytes=int(one * 3.5))
    exporter = server.exporter(s)
    server.status = 503
    for i in range(6):
        exporter.export(spans(f"s{i}"))
    files = spool_files(s)
    assert sum(f.stat().st_size for f in files) <= s.max_bytes
    assert len(files) == 3
    assert s.dropped == 3
    server.status = 202
    exporter.export([])
    assert server.names == ["s3", "s4", "s5"]  # the newest were kept


def test_a_batch_bigger_than_the_whole_spool_is_lost(tmp_path: Path, server: Server) -> None:
    s = Spool(tmp_path / "spool", max_bytes=100)
    server.status = 503
    assert server.exporter(s).export(spans("a", "b")) is SpanExportResult.FAILURE
    assert spool_files(s) == []
    assert s.dropped == 2


def test_age_cap_deletes_expired_files(tmp_path: Path, server: Server) -> None:
    s = Spool(tmp_path / "spool", max_age_s=60)
    assert s.put([{"name": "fresh"}])
    old_ns = 1_000_000_000_000_000_000
    (s.directory / f"{old_ns:020d}-1-000000-2{SUFFIX}").write_text(
        json.dumps({"v": 1, "spans": [{"name": "old1"}, {"name": "old2"}]})
    )
    server.exporter(s).export([])
    assert server.names == ["fresh"]
    assert s.dropped == 2
    assert spool_files(s) == []


def test_stale_temporary_files_are_cleaned_up(spool: Spool) -> None:
    assert spool.put([{"name": "a"}])
    stale = spool.directory / ".tmp-crashed-mid-write"
    stale.write_text("{")
    os.utime(stale, (1, 1))
    assert spool.put([{"name": "b"}])
    assert not stale.exists()


def test_disk_full_is_fail_open(
    spool: Spool, server: Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def full(fd: int, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_spool.os, "write", full)
    exporter = server.exporter(spool)
    server.status = 503
    with caplog.at_level("WARNING"):
        assert exporter.export(spans("a")) is SpanExportResult.FAILURE
        assert exporter.export(spans("b")) is SpanExportResult.FAILURE
    assert list(spool.directory.iterdir()) == []  # no half-written file left behind
    assert spool.dropped == 2
    assert caplog.text.count("No space left") == 1  # warned once


def test_unwritable_directory_is_fail_open(tmp_path: Path, server: Server) -> None:
    d = tmp_path / "ro"
    d.mkdir(mode=0o700)
    d.chmod(0o500)
    try:
        s = Spool(d)
        server.status = 503
        assert server.exporter(s).export(spans("a")) is SpanExportResult.FAILURE
        assert s.dropped == 1
    finally:
        d.chmod(0o700)
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    s = Spool(blocked / "spool")
    assert not s.put([{"name": "a"}])
    assert s.dropped == 1


def test_corrupt_files_are_skipped_and_deleted(
    spool: Spool, server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    assert spool.put([{"name": "good"}])
    d = spool.directory
    born = 2_000_000_000_000_000_000
    for i, content in enumerate(
        [b"{not json", b'{"v": 1, "spans": [1, 2]}', b'{"v": 99}', b"\xff"]
    ):
        (d / f"{born + i:020d}-1-000000-1{SUFFIX}").write_bytes(content)
    (d / "notes.txt").write_text("not ours")  # unrelated files are left alone
    with caplog.at_level("WARNING"):
        assert server.exporter(spool).export([]) is SpanExportResult.SUCCESS
    assert server.names == ["good"]
    assert spool_files(spool) == []
    assert (d / "notes.txt").exists()
    assert spool.dropped == 4
    assert caplog.text.count("skipping a corrupt") == 1


@pytest.fixture
def c(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_crash_restart_replays_spooled_spans(tmp_path: Path, c: TestClient) -> None:
    spool_dir = tmp_path / "spool"
    child = textwrap.dedent(
        f"""
        import os, sys
        import agentflight
        agentflight.configure("http://127.0.0.1:9", spool_dir={str(spool_dir)!r}, timeout=0.5)
        with agentflight.run("before-crash") as r:
            agentflight.decision("step", 1)
        assert agentflight.flush(timeout_millis=10_000)
        print(r.run_id)
        sys.stdout.flush()
        os._exit(1)  # crash: no shutdown, no atexit
        """
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    out = subprocess.run(
        [sys.executable, "-c", child], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 1, out.stderr
    crashed_run = out.stdout.strip()
    assert Spool(spool_dir).pending_spans() == 2  # the decision and the run span

    # A new process starts with the server back up: its first export sends the spool.
    exporter = AgentFlightExporter("http://testserver", client=c, spool=Spool(spool_dir))
    agentflight.configure(exporter=exporter, batch=True)
    with agentflight.run("after-restart"):
        pass
    assert agentflight.flush(timeout_millis=10_000)
    run = c.get(f"/v1/runs/{crashed_run}").json()
    assert run["status"] == "ok"
    assert run["event_count"] == 2
    assert Spool(spool_dir).pending_spans() == 0


def test_spool_never_runs_on_the_agent_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: set[str] = set()
    put, replay = Spool.put, Spool.replay

    def spy_put(self: Spool, items: list[dict[str, Any]]) -> bool:
        threads.add(threading.current_thread().name)
        return put(self, items)

    def spy_replay(self: Spool) -> Any:
        threads.add(threading.current_thread().name)
        return replay(self)

    monkeypatch.setattr(Spool, "put", spy_put)
    monkeypatch.setattr(Spool, "replay", spy_replay)
    rec = agentflight.configure("http://127.0.0.1:9", spool_dir=tmp_path / "s", timeout=0.5)
    assert rec.spool is not None
    for n in range(3):
        with agentflight.run(f"r{n}"):
            agentflight.decision("d", n)
    assert agentflight.flush(timeout_millis=10_000)
    assert threads
    assert threading.current_thread().name not in threads
    assert rec.spool.pending_spans() == 6


def test_spool_is_off_by_default_and_needs_batch_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert agentflight.configure("http://127.0.0.1:9").spool is None
    with caplog.at_level("WARNING"):
        rec = agentflight.configure("http://127.0.0.1:9", batch=False, spool_dir=tmp_path)
    assert rec.spool is None
    assert "spool needs batch=True" in caplog.text
    monkeypatch.setenv("AGENTFLIGHT_SPOOL_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("AGENTFLIGHT_SPOOL_MAX_BYTES", "4096")
    rec = agentflight.configure("http://127.0.0.1:9")
    assert rec.spool is not None
    assert rec.spool.directory == tmp_path / "env"
    assert rec.spool.max_bytes == 4096


def test_failed_run_span_is_spooled_and_closes_the_run_later(tmp_path: Path, c: TestClient) -> None:
    spool_dir = tmp_path / "spool"
    agentflight.configure("http://127.0.0.1:9", spool_dir=spool_dir, timeout=0.5)
    with agentflight.run("offline") as r:
        pass
    assert agentflight.flush(timeout_millis=10_000)
    agentflight.shutdown()
    assert Spool(spool_dir).pending_spans() == 1  # the run span, from the run lane

    exporter = AgentFlightExporter("http://testserver", client=c, spool=Spool(spool_dir))
    assert exporter.export([]) is SpanExportResult.SUCCESS
    assert c.get(f"/v1/runs/{r.run_id}").json()["status"] == "ok"
