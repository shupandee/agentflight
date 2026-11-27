"""Measure how much time AgentFlight's recorder adds to an agent run.

    python scripts/bench_overhead.py              # full run, prints a Markdown report
    python scripts/bench_overhead.py --quick      # fewer runs, for a smoke check

Method
------
The "agent" is a loop of ``STEPS`` model calls and ``STEPS`` tool calls that do no real
work (they return a canned string), so the measured time is almost all recording
overhead rather than agent work. One run is 1 run span + ``2 * STEPS`` child spans.

Each scenario runs the same agent ``--runs`` times after ``--warmup`` untimed runs and
times every run with ``time.perf_counter_ns`` on the agent thread. Reported: median and
95th percentile per run, and the median added time per span versus the uninstrumented
baseline. Scenarios:

* ``baseline``: the same functions with no AgentFlight code at all.
* ``sdk, no exporter``: spans are built (attributes, payload capture, truncation) and
  dropped. The cost of the instrumentation itself.
* ``sdk, serialize only``: plus converting every span to the ingest JSON on the agent
  thread (``span_to_dict`` and ``json.dumps``), without network.
* ``sdk, HTTP, batch=True, queue drained every 100 runs``: the default setup, exporting to
  a real AgentFlight server (uvicorn + SQLite in a temp dir) from OpenTelemetry's
  background thread. Every 100 runs the benchmark waits, untimed, for the queue to drain,
  as it would between real model calls, so no span is dropped. Agent-thread time only.
* ``sdk, HTTP, batch=True, unpaced``: the same with no pauses. A loop with no real work
  makes spans faster than the server stores them, OpenTelemetry's queue (2,048 spans)
  fills, and spans are dropped; the "spans stored" column shows how many.
* ``sdk, HTTP, batch=False``: every span is POSTed synchronously when it ends.

For the HTTP scenarios the benchmark counts the spans the server stored (warm-up runs
included) by reading its database at the end.

Each scenario is measured with small payloads (about 100 characters per message) and
large ones (8,000 characters). The server runs in its own process; on Linux, the
benchmark and the server are pinned to different CPUs. The machine, Python version,
package versions and load average are printed with the results; nothing is estimated.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from sqlalchemy import create_engine, text

import agentflight
from agentflight.exporter import AgentFlightExporter, span_to_dict

ROOT = Path(__file__).resolve().parents[1]
STEPS = 5
KEY = "bench-" + "k" * 40


class SerializeOnly(SpanExporter):
    """Does the exporter's CPU work (span -> JSON bytes) and sends nothing."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        json.dumps({"spans": [span_to_dict(s) for s in spans]}).encode()
        return SpanExportResult.SUCCESS


def _plain_tool(query: str) -> str:
    return f"result for {query}"


@agentflight.tool(name="lookup")
def _recorded_tool(query: str) -> str:
    return f"result for {query}"


def baseline_run(payload: str) -> None:
    messages = [{"role": "user", "content": payload}]
    for i in range(STEPS):
        answer = f"answer {i} {payload}"
        _ = (messages, answer)
        _plain_tool(payload)


def recorded_run(payload: str, name: str = "bench") -> None:
    messages = [{"role": "user", "content": payload}]
    with agentflight.run(name, metadata={"bench": "overhead"}):
        for i in range(STEPS):
            with agentflight.llm_call("bench-model", messages) as call:
                call.record(f"answer {i} {payload}", tokens_in=100, tokens_out=20, cost_usd=0.001)
            _recorded_tool(payload)


@dataclass
class Result:
    scenario: str
    payload: str
    times_ns: list[int]
    flush_ms: float | None = None
    run_name: str | None = None  # HTTP scenarios: the run name, to count what arrived
    spans_sent: int = 0
    spans_stored: int | None = None

    @property
    def median_us(self) -> float:
        return statistics.median(self.times_ns) / 1000

    @property
    def p95_us(self) -> float:
        return statistics.quantiles(self.times_ns, n=20)[18] / 1000


def measure(
    fn: Callable[[str], None],
    payload: str,
    runs: int,
    warmup: int,
    *,
    flush_every: int | None = None,
) -> list[int]:
    """Time ``runs`` calls of ``fn``. With ``flush_every``, wait for the exporter's queue to
    drain (untimed) every that many runs, so it never overflows."""
    for _ in range(warmup):
        fn(payload)
    agentflight.flush()
    out = []
    for i in range(runs):
        start = time.perf_counter_ns()
        fn(payload)
        out.append(time.perf_counter_ns() - start)
        if flush_every and (i + 1) % flush_every == 0:
            agentflight.flush()
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cpus() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return []


@contextlib.contextmanager
def server(server_cpus: list[int]) -> Iterator[tuple[str, Path]]:
    with tempfile.TemporaryDirectory() as tmp:
        port = _free_port()
        env = {
            **os.environ,
            "AGENTFLIGHT_DATABASE_URL": f"sqlite:///{tmp}/bench.db",
            "AGENTFLIGHT_API_KEY": KEY,
        }
        env.pop("AGENTFLIGHT_AUTH_DISABLED", None)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        cmd = [sys.executable, "-m", "uvicorn", "agentflight.server.main:app"]
        cmd += ["--port", str(port), "--log-level", "warning"]
        if server_cpus and sys.platform.startswith("linux"):
            cmd = ["taskset", "-c", ",".join(map(str, server_cpus)), *cmd]
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(200):
                with contextlib.suppress(httpx.HTTPError):
                    if httpx.get(f"{url}/ready", timeout=1).status_code == 200:
                        break
                time.sleep(0.05)
            else:
                raise RuntimeError("server did not start")
            yield url, Path(tmp) / "bench.db"
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def _no_exporter() -> None:
    # configure() always exports somewhere; a bare Recorder builds spans and drops them.
    agentflight.shutdown()
    agentflight.sdk._recorder = agentflight.sdk.Recorder()


def machine_info() -> list[str]:
    cpu = platform.processor() or platform.machine()
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    versions = ", ".join(
        f"{p} {metadata.version(p)}"
        for p in ("agentflight", "opentelemetry-sdk", "httpx", "fastapi", "uvicorn")
    )
    return [
        f"- Machine: {platform.system()} {platform.release()} ({platform.machine()}), {cpu}, "
        f"{os.cpu_count()} CPUs",
        f"- Python: {platform.python_implementation()} {platform.python_version()}",
        f"- Packages: {versions}",
        "- Load average at start (1/5/15 min): "
        + (" / ".join(f"{x:.2f}" for x in load) if load else "n/a"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=2000, help="timed runs per scenario")
    parser.add_argument("--warmup", type=int, default=200, help="untimed runs first")
    parser.add_argument("--quick", action="store_true", help="--runs 200 --warmup 20")
    parser.add_argument("--json", type=Path, help="also write raw results here")
    args = parser.parse_args()
    if args.quick:
        args.runs, args.warmup = 200, 20

    cpus = _cpus()
    bench_cpus, server_cpus = (cpus[:1], cpus[1:]) if len(cpus) > 1 else (cpus, cpus)
    if bench_cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, bench_cpus)

    info = machine_info()
    payloads = {"small (~100 chars)": "p" * 100, "large (8,000 chars)": "p" * 8_000}
    results: list[Result] = []
    spans_per_run = 1 + 2 * STEPS
    with server(server_cpus) as (url, db):

        def http(batch: bool) -> Callable[[], object]:
            return lambda: agentflight.configure(
                exporter=AgentFlightExporter(url, api_key=KEY), batch=batch
            )

        # (name, setup, flush_every): flush_every drains the batch queue between runs,
        # untimed, so it never overflows; None lets a tight loop outrun the exporter.
        setups: list[tuple[str, Callable[[], object], int | None]] = [
            ("sdk, no exporter", _no_exporter, None),
            (
                "sdk, serialize only",
                lambda: agentflight.configure(exporter=SerializeOnly(), batch=False),
                None,
            ),
            ("sdk, HTTP, batch=True, queue drained every 100 runs", http(True), 100),
            ("sdk, HTTP, batch=True, unpaced", http(True), None),
            ("sdk, HTTP, batch=False", http(False), None),
        ]
        for p_index, (label, payload) in enumerate(payloads.items()):
            results.append(
                Result("baseline", label, measure(baseline_run, payload, args.runs, args.warmup))
            )
            for s_index, (name, setup, flush_every) in enumerate(setups):
                run_name = f"bench-{p_index}-{s_index}"
                setup()
                times = measure(
                    functools.partial(recorded_run, name=run_name),
                    payload,
                    args.runs,
                    args.warmup,
                    flush_every=flush_every,
                )
                start = time.perf_counter()
                agentflight.flush()
                flush_ms = (time.perf_counter() - start) * 1000
                agentflight.shutdown()
                is_http = "HTTP" in name
                results.append(
                    Result(
                        name,
                        label,
                        times,
                        flush_ms if "batch=True" in name else None,
                        run_name if is_http else None,
                        (args.runs + args.warmup) * spans_per_run if is_http else 0,
                    )
                )
        time.sleep(0.5)
        engine = create_engine(f"sqlite:///{db}")
        with engine.connect() as conn:
            counts: dict[str, int] = dict(
                conn.execute(
                    text(
                        "SELECT runs.name, count(*) FROM events "
                        "JOIN runs ON runs.id = events.run_id GROUP BY runs.name"
                    )
                ).all()
            )
        engine.dispose()
        for r in results:
            if r.run_name is not None:
                r.spans_stored = int(counts.get(r.run_name, 0))

    print("## AgentFlight recorder overhead\n")
    print(*info, sep="\n")
    print(
        f"- Runs: {args.runs} timed per scenario after {args.warmup} warm-up runs; "
        f"{spans_per_run} spans per run ({STEPS} model calls, {STEPS} tool calls)"
    )
    print(f"- CPUs: benchmark pinned to {bench_cpus or 'any'}, server to {server_cpus or 'any'}")
    print()
    print(
        "| Payload | Scenario | Median per run | p95 per run | Added per span (median) "
        "| Spans stored |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    for label in payloads:
        base = next(r for r in results if r.payload == label and r.scenario == "baseline")
        for r in (r for r in results if r.payload == label):
            added = (
                "—"
                if r.scenario == "baseline"
                else f"{(r.median_us - base.median_us) / spans_per_run:.1f} µs"
            )
            stored = "—" if r.spans_stored is None else f"{r.spans_stored:,} of {r.spans_sent:,}"
            print(
                f"| {label} | {r.scenario} | {r.median_us:,.1f} µs | {r.p95_us:,.1f} µs "
                f"| {added} | {stored} |"
            )
    flushes = [r for r in results if r.flush_ms is not None]
    if flushes:
        print()
        for r in flushes:
            print(
                f"- {r.payload}, {r.scenario}: flushing the queue after the last run took "
                f"{r.flush_ms:,.1f} ms."
            )
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "machine": info,
                    "runs": args.runs,
                    "warmup": args.warmup,
                    "spans_per_run": spans_per_run,
                    "results": [
                        {
                            "scenario": r.scenario,
                            "payload": r.payload,
                            "median_us": r.median_us,
                            "p95_us": r.p95_us,
                            "flush_ms": r.flush_ms,
                            "spans_sent": r.spans_sent,
                            "spans_stored": r.spans_stored,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
