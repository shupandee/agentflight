"""Measure how much time AgentFlight's recorder adds to an agent run, and how fast the
server ingests spans.

    python scripts/bench_overhead.py               # 5 repeats, prints a Markdown report
    python scripts/bench_overhead.py --quick       # 1 short repeat, for a smoke check

Method
------
The "agent" is a loop of ``STEPS`` model calls and ``STEPS`` tool calls that do no real
work (they return a canned string), so the measured time is almost all recording
overhead rather than agent work. One run is 1 run span + ``2 * STEPS`` child spans.

Everything is repeated ``--repeats`` times, each repeat against a fresh server (uvicorn +
SQLite in a temp dir, its own process). In each repeat every scenario runs the agent
``--runs`` times (``--sync-runs`` for ``batch=False``, which is slow) after
``--warmup`` untimed runs, timing each run on the agent thread with
``time.perf_counter_ns``. The report gives, per scenario, the median of the per-repeat
medians, the spread of those medians (min-max), the median p95, and the median added per
span versus the uninstrumented baseline. Scenarios:

* ``baseline``: the same functions with no AgentFlight code at all.
* ``sdk, no exporter``: spans are built (attributes, payload capture, truncation) and
  dropped. The cost of the instrumentation itself.
* ``sdk, serialize only``: plus converting every span to the ingest JSON on the agent
  thread (``span_to_dict`` and ``json.dumps``), without network.
* ``sdk, HTTP, batch=True, queue drained every 100 runs``: the default setup, exporting
  from OpenTelemetry's background thread. Every 100 runs the benchmark waits, untimed,
  for the queue to drain, as it would between real model calls. Agent-thread time only.
* ``sdk, HTTP, batch=True, unpaced``: the same with no pauses, so the loop makes spans
  faster than the server stores them and the queue (2,048 spans) fills.
* ``sdk, HTTP, batch=False``: every span is POSTed synchronously when it ends.

For the HTTP scenarios the benchmark counts the spans the server stored (warm-up runs
included) by reading its database, and the spans the SDK reported dropping.

Ingest rate: spans recorded in advance are sent to the server in 512-span batches (the
batch processor's size), one after another from one thread, as the batch processor does;
the rate is spans stored per second of wall time.

The machine, Python and package versions and load average are printed with the results;
nothing is estimated.
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
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from sqlalchemy import create_engine, text

import agentflight
from agentflight.exporter import AgentFlightExporter, span_to_dict

ROOT = Path(__file__).resolve().parents[1]
STEPS = 5
SPANS_PER_RUN = 1 + 2 * STEPS
KEY = "bench-" + "k" * 40
INGEST_BATCH = 512
PAYLOADS = {"small (~100 chars)": "p" * 100, "large (8,000 chars)": "p" * 8_000}
DRAINED = "sdk, HTTP, batch=True, queue drained every 100 runs"
UNPACED = "sdk, HTTP, batch=True, unpaced"
SYNC = "sdk, HTTP, batch=False"


class SerializeOnly(SpanExporter):
    """Does the exporter's CPU work (span -> JSON bytes) and sends nothing."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        json.dumps({"spans": [span_to_dict(s) for s in spans]}).encode()
        return SpanExportResult.SUCCESS


class Collect(SpanExporter):
    """Keeps finished spans, to send them to the server later at full speed."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
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
class Sample:
    """One scenario in one repeat."""

    median_us: float
    p95_us: float
    spans_sent: int = 0
    spans_stored: int | None = None
    sdk_dropped: int | None = None  # spans the SDK counted as dropped
    on_runs: int | None = None  # of those, recorded on the runs' run spans
    runs_sent: int = 0
    runs_closed: int | None = None  # runs the server has with a final status
    wall_s: float = 0.0  # warm-up + timed runs + final flush


@dataclass
class Repeat:
    samples: dict[tuple[str, str], Sample] = field(default_factory=dict)
    ingest_rate: dict[str, float] = field(default_factory=dict)  # payload -> spans/s
    load: tuple[float, float, float] | None = None


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


def _sample(times: list[int]) -> Sample:
    return Sample(
        median_us=statistics.median(times) / 1000,
        p95_us=statistics.quantiles(times, n=20)[18] / 1000,
    )


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


def _stored(db: Path) -> dict[str, tuple[int, int, int]]:
    """run name -> (spans stored, dropped spans recorded on its run spans, runs closed)."""
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        spans: dict[str, int] = dict(
            conn.execute(
                text(
                    "SELECT runs.name, count(*) FROM events "
                    "JOIN runs ON runs.id = events.run_id GROUP BY runs.name"
                )
            ).all()
        )
        dropped: dict[str, int | None] = dict(
            conn.execute(text("SELECT name, sum(dropped_spans) FROM runs GROUP BY name")).all()
        )
        closed: dict[str, int] = dict(
            conn.execute(
                text("SELECT name, count(*) FROM runs WHERE status != 'running' GROUP BY name")
            ).all()
        )
    engine.dispose()
    return {
        name: (int(spans.get(name, 0)), int(dropped.get(name) or 0), int(closed.get(name, 0)))
        for name in dropped
        if name is not None
    }


def ingest_rate(url: str, payload: str, batches: int) -> float:
    """Spans per second the server stores from back-to-back 512-span batches."""
    collect = Collect()
    agentflight.configure(exporter=collect, batch=False)
    while len(collect.spans) < batches * INGEST_BATCH:
        recorded_run(payload, "ingest")
    agentflight.shutdown()
    spans = collect.spans[: batches * INGEST_BATCH]
    exporter = AgentFlightExporter(url, api_key=KEY, timeout=60)
    start = time.perf_counter()
    for i in range(0, len(spans), INGEST_BATCH):
        if exporter.export(spans[i : i + INGEST_BATCH]) is not SpanExportResult.SUCCESS:
            raise RuntimeError("ingest batch failed")
    elapsed = time.perf_counter() - start
    exporter.shutdown()
    return len(spans) / elapsed


def one_repeat(args: argparse.Namespace, server_cpus: list[int]) -> Repeat:
    rep = Repeat(load=os.getloadavg() if hasattr(os, "getloadavg") else None)
    with server(server_cpus) as (url, db):

        def http(batch: bool) -> Callable[[], object]:
            return lambda: agentflight.configure(
                exporter=AgentFlightExporter(url, api_key=KEY), batch=batch
            )

        # (name, setup, flush_every, runs, warmup)
        setups: list[tuple[str, Callable[[], object], int | None, int, int]] = [
            ("sdk, no exporter", _no_exporter, None, args.runs, args.warmup),
            (
                "sdk, serialize only",
                lambda: agentflight.configure(exporter=SerializeOnly(), batch=False),
                None,
                args.runs,
                args.warmup,
            ),
            (DRAINED, http(True), 100, args.runs, args.warmup),
            (UNPACED, http(True), None, args.runs, args.warmup),
            (SYNC, http(False), None, args.sync_runs, max(1, args.warmup // 10)),
        ]
        names: dict[tuple[str, str], tuple[str, int]] = {}
        for p_index, (label, payload) in enumerate(PAYLOADS.items()):
            rep.samples[(label, "baseline")] = _sample(
                measure(baseline_run, payload, args.runs, args.warmup)
            )
            for s_index, (name, setup, flush_every, runs, warmup) in enumerate(setups):
                run_name = f"bench-{p_index}-{s_index}"
                setup()
                wall = time.perf_counter()
                times = measure(
                    functools.partial(recorded_run, name=run_name),
                    payload,
                    runs,
                    warmup,
                    flush_every=flush_every,
                )
                agentflight.flush()
                sample = _sample(times)
                sample.wall_s = time.perf_counter() - wall
                if "HTTP" in name:
                    sample.spans_sent = (runs + warmup) * SPANS_PER_RUN
                    sample.runs_sent = runs + warmup
                    sample.sdk_dropped = agentflight.get_recorder().dropped_spans
                    names[(label, name)] = (run_name, 0)
                agentflight.shutdown()
                rep.samples[(label, name)] = sample
            rep.ingest_rate[label] = ingest_rate(url, payload, args.ingest_batches)
        time.sleep(0.5)
        stored = _stored(db)
        for key, (run_name, _) in names.items():
            spans, on_runs, closed = stored.get(run_name, (0, 0, 0))
            rep.samples[key].spans_stored = spans
            rep.samples[key].on_runs = on_runs
            rep.samples[key].runs_closed = closed
    return rep


def machine_info() -> list[str]:
    cpu = platform.processor() or platform.machine()
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    virt = ""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("Hypervisor vendor:"):
                virt = f", VM ({line.split(':', 1)[1].strip()})"
    versions = ", ".join(
        f"{p} {metadata.version(p)}"
        for p in ("agentflight", "opentelemetry-sdk", "httpx", "fastapi", "uvicorn")
    )
    return [
        f"- Machine: {platform.system()} {platform.release()} ({platform.machine()}), {cpu}, "
        f"{os.cpu_count()} CPUs{virt}",
        f"- Python: {platform.python_implementation()} {platform.python_version()}",
        f"- Packages: {versions}",
    ]


def _range(values: list[float], fmt: str = "{:,.1f}") -> str:
    return f"{fmt.format(min(values))} to {fmt.format(max(values))}"


def report(args: argparse.Namespace, info: list[str], repeats: list[Repeat], cpus: str) -> Any:
    loads = [r.load[0] for r in repeats if r.load]
    print("## AgentFlight recorder overhead\n")
    print(*info, sep="\n")
    if loads:
        print(f"- Load average (1 min) at the start of each repeat: {_range(loads, '{:.2f}')}")
    print(
        f"- {len(repeats)} repeat(s), each on a fresh server. Per repeat and scenario: "
        f"{args.runs} timed runs after {args.warmup} warm-up runs ({args.sync_runs} for "
        f"batch=False); {SPANS_PER_RUN} spans per run ({STEPS} model calls, {STEPS} tool "
        "calls)"
    )
    print(f"- CPUs: {cpus}\n")
    print(
        "| Payload | Scenario | Median per run | Spread of medians | p95 per run "
        "| Added per span | Spans stored | Dropped (SDK count / on runs) | Runs closed |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    rows: list[dict[str, Any]] = []
    scenarios = list(dict.fromkeys(name for (_, name) in repeats[0].samples))
    for label in PAYLOADS:
        base = statistics.median(r.samples[(label, "baseline")].median_us for r in repeats)
        for name in scenarios:
            samples = [r.samples[(label, name)] for r in repeats]
            medians = [s.median_us for s in samples]
            median = statistics.median(medians)
            p95 = statistics.median(s.p95_us for s in samples)
            added = None if name == "baseline" else (median - base) / SPANS_PER_RUN
            sent = sum(s.spans_sent for s in samples)
            stored = (
                sum(s.spans_stored or 0 for s in samples)
                if samples[0].spans_stored is not None
                else None
            )
            dropped = (
                sum(s.sdk_dropped or 0 for s in samples)
                if samples[0].sdk_dropped is not None
                else None
            )
            on_runs = sum(s.on_runs or 0 for s in samples)
            runs_sent = sum(s.runs_sent for s in samples)
            closed = (
                sum(s.runs_closed or 0 for s in samples)
                if samples[0].runs_closed is not None
                else None
            )
            print(
                f"| {label} | {name} | {median:,.1f} µs | {_range(medians)} µs "
                f"| {p95:,.1f} µs | {'—' if added is None else f'{added:,.1f} µs'} "
                f"| {'—' if stored is None else f'{stored:,} of {sent:,}'} "
                f"| {'—' if dropped is None else f'{dropped:,} / {on_runs:,}'} "
                f"| {'—' if closed is None else f'{closed:,} of {runs_sent:,}'} |"
            )
            rows.append(
                {
                    "payload": label,
                    "scenario": name,
                    "median_us": median,
                    "repeat_medians_us": medians,
                    "p95_us": p95,
                    "added_per_span_us": added,
                    "spans_sent": sent,
                    "spans_stored": stored,
                    "sdk_dropped": dropped,
                    "dropped_on_runs": on_runs,
                    "runs_sent": runs_sent,
                    "runs_closed": closed,
                }
            )
    print("\n### Server ingest rate\n")
    print(
        "| Payload | Ingest, spans/s (median) | Spread | Unpaced loop: spans made/s "
        "| Unpaced loop: spans stored/s |"
    )
    print("| --- | ---: | ---: | ---: | ---: |")
    ingest: dict[str, Any] = {}
    for label in PAYLOADS:
        rates = [r.ingest_rate[label] for r in repeats]
        unpaced = statistics.median(r.samples[(label, UNPACED)].median_us for r in repeats)
        agent_rate = SPANS_PER_RUN / (unpaced / 1e6)
        delivered = statistics.median(
            (r.samples[(label, UNPACED)].spans_stored or 0) / r.samples[(label, UNPACED)].wall_s
            for r in repeats
        )
        print(
            f"| {label} | {statistics.median(rates):,.0f} | {_range(rates, '{:,.0f}')} "
            f"| {agent_rate:,.0f} | {delivered:,.0f} |"
        )
        ingest[label] = {
            "median_spans_per_s": statistics.median(rates),
            "repeat_rates": rates,
            "unpaced_spans_made_per_s": agent_rate,
            "unpaced_spans_stored_per_s": delivered,
        }
    print(
        f"\n(Ingest: {args.ingest_batches} batches of {INGEST_BATCH} spans per payload and "
        "repeat, sent back to back from one thread with nothing else running in the "
        "process. Unpaced loop: spans made per second on the agent thread (median run "
        "time), and spans the server stored per second of the scenario's wall time, "
        "while the exporter thread shared the benchmark's one CPU with the agent.)"
    )
    return {"rows": rows, "ingest": ingest, "loads": loads}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repeats", type=int, default=5, help="whole-benchmark repeats")
    parser.add_argument("--runs", type=int, default=2000, help="timed runs per scenario")
    parser.add_argument("--sync-runs", type=int, default=200, help="timed runs, batch=False")
    parser.add_argument("--warmup", type=int, default=200, help="untimed runs first")
    parser.add_argument("--ingest-batches", type=int, default=20, help="512-span batches")
    parser.add_argument("--quick", action="store_true", help="1 short repeat")
    parser.add_argument("--json", type=Path, help="also write raw results here")
    args = parser.parse_args()
    if args.quick:
        args.repeats, args.runs, args.sync_runs, args.warmup, args.ingest_batches = (
            1,
            200,
            20,
            20,
            4,
        )

    cpus = _cpus()
    bench_cpus, server_cpus = (cpus[:1], cpus[1:]) if len(cpus) > 1 else (cpus, cpus)
    if bench_cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, bench_cpus)
    pinning = f"benchmark pinned to {bench_cpus or 'any'}, server to {server_cpus or 'any'}"

    info = machine_info()
    repeats = []
    for i in range(args.repeats):
        print(f"repeat {i + 1}/{args.repeats}...", file=sys.stderr, flush=True)
        repeats.append(one_repeat(args, server_cpus))
    data = report(args, info, repeats, pinning)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "machine": info,
                    "repeats": args.repeats,
                    "runs": args.runs,
                    "sync_runs": args.sync_runs,
                    "warmup": args.warmup,
                    "spans_per_run": SPANS_PER_RUN,
                    **data,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
