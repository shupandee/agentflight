"""The overhead benchmark script runs end to end (a few runs only) and reports real counts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_overhead_benchmark_runs(tmp_path: Path) -> None:
    out = tmp_path / "bench.json"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/bench_overhead.py",
            *("--runs", "4", "--warmup", "1", "--json", str(out)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "| Payload | Scenario |" in proc.stdout and "Python: CPython" in proc.stdout
    data = json.loads(out.read_text())
    assert data["runs"] == 4 and data["spans_per_run"] == 11
    http = [r for r in data["results"] if "HTTP" in r["scenario"]]
    assert len(http) == 6
    # A handful of runs can't overflow the queue: everything sent is stored.
    assert all(r["spans_stored"] == r["spans_sent"] == 5 * 11 for r in http)
