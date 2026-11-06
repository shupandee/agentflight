"""Logical error de-duplication via failure ids."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agentflight


class Slotted(Exception):
    __slots__ = ()


def _run(recorded: TestClient, run_id: str) -> dict:
    agentflight.flush()
    return recorded.get(f"/v1/runs/{run_id}").json()


def _fids(run: dict) -> list[str]:
    return [
        e["attributes"]["agentflight.failure.id"] for e in run["events"] if e["status"] == "error"
    ]


def test_tool_error_logged_by_agent_counts_once(recorded: TestClient) -> None:
    @agentflight.tool
    def broken() -> None:
        raise LookupError("no data")

    with agentflight.run("r") as r:
        try:
            broken()
        except LookupError as exc:
            agentflight.error(exc)
    run = _run(recorded, r.run_id)
    assert (run["errors"], run["error_events"]) == (1, 2)
    assert len(set(_fids(run))) == 1


def test_tool_error_failing_the_run_counts_once(recorded: TestClient) -> None:
    @agentflight.tool
    def broken() -> None:
        raise KeyError("k")

    with pytest.raises(KeyError), agentflight.run("r") as r:
        try:
            broken()
        except KeyError as exc:
            agentflight.error(exc)
            raise
    run = _run(recorded, r.run_id)
    assert run["status"] == "error"
    assert (run["errors"], run["error_events"]) == (1, 3)  # tool + log + run, one failure


def test_wrapped_reraise_inherits_failure(recorded: TestClient) -> None:
    @agentflight.tool
    def broken() -> None:
        raise ValueError("inner")

    with pytest.raises(RuntimeError), agentflight.run("r") as r:
        try:
            broken()
        except ValueError as exc:
            raise RuntimeError("wrapped") from exc
    run = _run(recorded, r.run_id)
    assert (run["errors"], run["error_events"]) == (1, 2)


def test_independent_failures_count_separately(recorded: TestClient) -> None:
    @agentflight.tool
    def broken(n: int) -> None:
        raise ValueError(f"fail {n}")

    with agentflight.run("r") as r:
        for n in range(3):
            try:
                broken(n)
            except ValueError as exc:
                agentflight.error(exc)
        agentflight.error(TimeoutError("unrelated"))
    run = _run(recorded, r.run_id)
    assert (run["errors"], run["error_events"]) == (4, 7)
    assert len(set(_fids(run))) == 4


def test_run_failure_alone_counts_once(recorded: TestClient) -> None:
    with pytest.raises(ZeroDivisionError), agentflight.run("r") as r:
        _ = 1 / 0
    run = _run(recorded, r.run_id)
    assert (run["errors"], run["error_events"]) == (1, 1)


def test_failure_id_works_for_slotted_exceptions() -> None:
    exc = Slotted()
    assert agentflight.failure_id(exc) == agentflight.failure_id(exc)
    assert agentflight.failure_id(exc) != agentflight.failure_id(Slotted())


def test_events_without_failure_id_count_individually(client: TestClient) -> None:
    trace = "c" * 32
    spans = [
        {
            "trace_id": trace,
            "span_id": f"{i:016x}",
            "name": "legacy",
            "start_time_ns": i,
            "end_time_ns": i + 1,
            "status": "error",
            "attributes": {"agentflight.kind": "tool"},
        }
        for i in range(1, 3)
    ]
    client.post("/v1/spans", json={"spans": spans})
    assert client.get(f"/v1/runs/{trace}").json()["errors"] == 2
