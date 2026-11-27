"""Stored run totals: recomputed per ingest batch, never incremented."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

T = "c" * 32
TOTAL_KEYS = (
    "event_count",
    "llm_calls",
    "tool_calls",
    "errors",
    "error_events",
    "tokens_in",
    "tokens_out",
    "cost_usd",
)


def span(sid: str, kind: str, trace: str = T, **kw: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {"agentflight.kind": kind, **kw.pop("attributes", {})}
    base: dict[str, Any] = {
        "trace_id": trace,
        "span_id": sid * 16,
        "parent_span_id": None if kind == "run" else "0" * 16,
        "name": f"{kind}-{sid}",
        "start_time_ns": int(sid, 16) + 10,
        "end_time_ns": int(sid, 16) + 20,
        "status": "ok",
        "attributes": attrs,
    }
    base.update(kw)
    return base


def llm(sid: str, tin: int, tout: int, cost: float | None = None, **kw: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "agentflight.llm.model": "m-1",
        "agentflight.llm.tokens_in": tin,
        "agentflight.llm.tokens_out": tout,
    }
    if cost is not None:
        attrs["agentflight.llm.cost_usd"] = cost
    return span(sid, "llm", attributes=attrs, **kw)


def batch() -> list[dict[str, Any]]:
    fid = {"agentflight.failure.id": "f" * 32}
    return [
        span("0", "run", status="error", attributes=fid, start_time_ns=0, end_time_ns=100),
        llm("1", 10, 5, 0.25),
        llm("2", 7, 3),
        span("3", "tool", status="error", attributes={"agentflight.tool.name": "t", **fid}),
        span("4", "error", status="error", attributes=fid),
        span("5", "tool", status="error", attributes={"agentflight.tool.name": "u"}),
    ]


def totals(client: TestClient, run_id: str = T) -> dict[str, Any]:
    detail = client.get(f"/v1/runs/{run_id}").json()
    listed = next(r for r in client.get("/v1/runs").json()["runs"] if r["id"] == run_id)
    picked = {k: detail[k] for k in TOTAL_KEYS}
    assert picked == {k: listed[k] for k in TOTAL_KEYS}
    return picked


EXPECTED = {
    "event_count": 6,
    "llm_calls": 2,
    "tool_calls": 2,
    "errors": 2,  # one shared failure id + one event without an id
    "error_events": 4,
    "tokens_in": 17,
    "tokens_out": 8,
    "cost_usd": 0.25,
}


def test_totals_from_one_batch(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": batch()})
    assert totals(client) == EXPECTED


def test_same_batch_ingested_twice_gives_identical_counts(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": batch()})
    first = totals(client)
    client.post("/v1/spans", json={"spans": batch()})
    assert totals(client) == first == EXPECTED


def test_split_batches_give_the_same_totals(client: TestClient) -> None:
    spans = batch()
    for s in reversed(spans):  # children before the root, one span per request
        client.post("/v1/spans", json={"spans": [s]})
    assert totals(client) == EXPECTED


def test_updated_span_replaces_its_old_contribution(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": batch()})
    client.post("/v1/spans", json={"spans": [llm("1", 100, 50, 1.0, status="error")]})
    got = totals(client)
    assert (got["tokens_in"], got["tokens_out"], got["cost_usd"]) == (107, 53, 1.0)
    assert (got["errors"], got["error_events"]) == (3, 5)


def test_cost_is_null_when_no_call_reports_it(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": [span("0", "run"), llm("1", 1, 1)]})
    assert totals(client)["cost_usd"] is None


def test_malformed_usage_from_raw_senders_is_ignored(client: TestClient) -> None:
    bad = llm("1", 0, 0)
    bad["attributes"]["agentflight.llm.tokens_in"] = "lots"
    bad["attributes"]["agentflight.llm.cost_usd"] = [1]
    assert client.post("/v1/spans", json={"spans": [span("0", "run"), bad]}).status_code == 202
    got = totals(client)
    assert (got["tokens_in"], got["cost_usd"], got["llm_calls"]) == (0, None, 1)


def test_one_batch_updates_several_runs(client: TestClient) -> None:
    other = "d" * 32
    spans = [*batch(), span("0", "run", trace=other), llm("1", 1, 2, trace=other)]
    body = client.post("/v1/spans", json={"spans": spans}).json()
    assert body == {"accepted": 8, "runs": [T, other]}
    assert totals(client) == EXPECTED
    assert totals(client, other)["tokens_out"] == 2


@contextmanager
def statements(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_ingest_queries_do_not_grow_with_span_count(client: TestClient) -> None:
    engine = client.app.state.engine  # type: ignore[attr-defined]

    def selects(n: int, trace: str) -> int:
        spans = [span("0", "run", trace=trace)]
        spans += [span(f"{i:x}", "tool", trace=trace, span_id=f"{i:016x}") for i in range(1, n)]
        with statements(engine) as seen:
            assert client.post("/v1/spans", json={"spans": spans}).status_code == 202
        return sum(s.lstrip().upper().startswith("SELECT") for s in seen)

    assert selects(5, "1" * 32) == selects(200, "2" * 32)


def test_run_list_does_not_read_events(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": batch()})
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with statements(engine) as seen:
        assert client.get("/v1/runs").json()["total"] == 1
    assert seen
    assert not [s for s in seen if re.search(r"\b(FROM|JOIN)\s+events\b", s)]
