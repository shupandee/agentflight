"""Smoke test for the docker-compose stack (used by CI).

1. Auth: the API rejects missing and wrong keys.
2. An agent configured with a wrong key still completes, and nothing is persisted.
3. With the right key, examples/toy_agent.py's runs are persisted with the expected token,
   cost and error totals.
4. DELETE removes a run.

Needs AGENTFLIGHT_API_KEY set to the server's key.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ENDPOINT = os.environ.get("AGENTFLIGHT_ENDPOINT", "http://localhost:8000")
KEY = os.environ["AGENTFLIGHT_API_KEY"]
AUTH = {"Authorization": f"Bearer {KEY}"}
ROOT = Path(__file__).resolve().parents[1]
PRICE = 0.000002  # TOY_USD_PER_TOKEN in examples/toy_agent.py

# Deterministic output of the scripted fake model in the example.
EXPECTED = [
    {
        "question": "What is the weather in Delhi?",
        "tokens_in": 18,
        "tokens_out": 7,
        "errors": 0,
        "error_events": 0,
        "llm_calls": 2,
        "tool_calls": 1,
        "event_count": 7,
    },
    {
        "question": "What is the weather in Atlantis?",
        "tokens_in": 21,
        "tokens_out": 10,
        # The tool's LookupError and the agent logging it are one logical failure.
        "errors": 1,
        "error_events": 2,
        "llm_calls": 2,
        "tool_calls": 1,
        "event_count": 8,
    },
]


def wait_ready(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{ENDPOINT}/ready", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit(f"server at {ENDPOINT} not ready after {timeout}s")


def run_agent(key: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "toy_agent.py")],
        env={**os.environ, "AGENTFLIGHT_ENDPOINT": ENDPOINT, "AGENTFLIGHT_API_KEY": key},
        capture_output=True,
        text=True,
        check=True,  # the agent must succeed even when the recorder rejects it
    ).stdout
    print(out)
    run_ids = re.findall(r"run_id=([0-9a-f]{32})", out)
    assert len(run_ids) == 2, run_ids
    assert out.count("Final answer") == 2, out
    return run_ids


def check_auth() -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        resp = httpx.get(f"{ENDPOINT}/v1/runs", headers=headers, timeout=5)
        assert resp.status_code == 401, (headers.keys(), resp.status_code)
    assert httpx.get(f"{ENDPOINT}/v1/runs", headers=AUTH, timeout=5).status_code == 200
    print("ok auth: missing and wrong keys rejected, valid key accepted")


def main() -> None:
    wait_ready()
    check_auth()

    rejected = run_agent("wrong-key")
    for run_id in rejected:
        resp = httpx.get(f"{ENDPOINT}/v1/runs/{run_id}", headers=AUTH, timeout=5)
        assert resp.status_code == 404, (run_id, resp.status_code)
    print("ok agent with wrong key completed; nothing persisted")

    run_ids = run_agent(KEY)

    for run_id, want in zip(run_ids, EXPECTED, strict=True):
        resp = httpx.get(f"{ENDPOINT}/v1/runs/{run_id}", headers=AUTH, timeout=5)
        assert resp.status_code == 200, (run_id, resp.status_code, resp.text)
        run = resp.json()
        assert run["status"] == "ok", run["status"]
        assert run["metadata"] == {"question": want["question"]}, run["metadata"]
        for key in (
            "tokens_in",
            "tokens_out",
            "errors",
            "error_events",
            "llm_calls",
            "tool_calls",
            "event_count",
        ):
            assert run[key] == want[key], (run_id, key, run[key], want[key])
        cost = (want["tokens_in"] + want["tokens_out"]) * PRICE
        assert math.isclose(run["cost_usd"], cost, rel_tol=1e-9), (run["cost_usd"], cost)
        print(
            f"ok {run_id}: tokens {run['tokens_in']}/{run['tokens_out']}, "
            f"cost {run['cost_usd']:.6f}, errors {run['errors']}"
        )

    listing = httpx.get(f"{ENDPOINT}/v1/runs", headers=AUTH, timeout=5).json()
    assert {r["id"] for r in listing["runs"]} >= set(run_ids), listing

    url = f"{ENDPOINT}/v1/runs/{run_ids[0]}"
    assert httpx.delete(url, timeout=5).status_code == 401
    assert httpx.delete(url, headers=AUTH, timeout=5).status_code == 204
    assert httpx.get(url, headers=AUTH, timeout=5).status_code == 404
    print(f"ok deleted {run_ids[0]}")
    print("compose smoke test passed")


if __name__ == "__main__":
    main()
