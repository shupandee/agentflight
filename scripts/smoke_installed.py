"""Smoke test for an installed AgentFlight (e.g. a freshly built wheel in a clean venv).

Run it from outside the source tree, with ``agentflight[server]`` installed::

    python scripts/smoke_installed.py

It checks the version and the packaged data files, creates a SQLite database with
``agentflight-server migrate``, starts ``agentflight-server serve``, records a run with
the SDK, and reads it back through the API and the viewer. Exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from importlib import metadata, resources
from pathlib import Path


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {what}")
    print(f"ok: {what}")


def get(url: str, key: str | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    import agentflight

    here = Path(agentflight.__file__).resolve().parent
    check("site-packages" in str(here), f"agentflight imported from an install ({here})")
    check(agentflight.__version__ == metadata.version("agentflight"), "version matches metadata")
    print(f"   version {agentflight.__version__}")
    server = resources.files("agentflight.server")
    for path in (
        "migrations/env.py",
        "migrations/versions/0004_run_dropped_spans.py",
        "viewer/templates/run.html",
        "viewer/static/viewer.css",
    ):
        check(server.joinpath(path).is_file(), f"packaged {path}")
    check(resources.files("agentflight").joinpath("py.typed").is_file(), "packaged py.typed")

    beside = Path(sys.executable).parent / "agentflight-server"  # the venv, even if inactive
    cli = str(beside) if beside.exists() else shutil.which("agentflight-server")
    check(cli is not None, "agentflight-server is on PATH")
    assert cli is not None
    work = Path(tempfile.mkdtemp(prefix="agentflight-smoke-"))
    key = secrets.token_hex(32)
    env = dict(
        os.environ,
        AGENTFLIGHT_DATABASE_URL=f"sqlite:///{work / 'smoke.db'}",
        AGENTFLIGHT_API_KEY=key,
    )
    subprocess.run([cli, "migrate"], cwd=work, env=env, check=True, capture_output=True)
    check(True, "agentflight-server migrate")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [cli, "serve", "--port", str(port)],
        cwd=work,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if get(f"{base}/ready")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline or proc.poll() is not None:
                raise SystemExit("FAIL: server did not become ready")
            time.sleep(0.2)
        check(True, "server is ready")

        agentflight.configure(base, api_key=key)

        @agentflight.tool
        def add(a: int, b: int) -> int:
            return a + b

        with agentflight.run("smoke", metadata={"from": "smoke_installed"}) as run:
            agentflight.decision("plan", "add two numbers")
            check(add(2, 3) == 5, "a recorded tool returns its result")
        check(agentflight.flush(timeout_millis=10_000), "flush")
        agentflight.shutdown()

        status, body = get(f"{base}/v1/runs/{run.run_id}", key)
        check(status == 200, "GET /v1/runs/{id}")
        detail = json.loads(body)
        check(detail["status"] == "ok", "the run is closed with status ok")
        check(detail["event_count"] == 3, "the run has its 3 spans")
        check(get(f"{base}/v1/runs")[0] == 401, "the API needs the key")
        check(get(f"{base}/ui/login")[0] == 200, "viewer login page")
        status, css = get(f"{base}/ui/static/viewer.css")
        check(status == 200 and ".incomplete" in css, "viewer static files")
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(work, ignore_errors=True)
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
