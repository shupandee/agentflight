from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExportResult

import agentflight
from agentflight.exporter import AgentFlightExporter


def span(trace: str, sid: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "trace_id": trace,
        "span_id": sid,
        "name": f"s{sid}",
        "start_time_ns": 1,
        "end_time_ns": 2,
        "attributes": {},
    }
    base.update(kw)
    return base


T = "a" * 32


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_end_to_end_run_is_stored_with_ordered_events(recorded: TestClient) -> None:
    @agentflight.tool
    def search(query: str) -> list[str]:
        return [f"result for {query}"]

    with agentflight.run("e2e", metadata={"env": "test"}) as r:
        with agentflight.llm_call("toy", [{"role": "user", "content": "q"}]) as call:
            call.record("use search", tokens_in=5, tokens_out=2, cost_usd=0.01)
        agentflight.decision("next", "search")
        search("cats")
        with agentflight.llm_call("toy", []) as call:
            call.record("done", tokens_in=7, tokens_out=3, cost_usd=0.02)
    agentflight.flush()

    listing = recorded.get("/v1/runs").json()
    assert listing["total"] == 1
    summary = listing["runs"][0]
    assert summary["id"] == r.run_id
    assert summary["name"] == "e2e"
    assert summary["status"] == "ok"
    assert summary["metadata"] == {"env": "test"}
    assert (summary["llm_calls"], summary["tool_calls"], summary["errors"]) == (2, 1, 0)
    assert (summary["tokens_in"], summary["tokens_out"]) == (12, 5)
    assert summary["cost_usd"] == pytest.approx(0.03)

    detail = recorded.get(f"/v1/runs/{r.run_id}").json()
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["run", "llm", "decision", "tool", "llm"]
    starts = [e["start_time_ns"] for e in detail["events"]]
    assert starts == sorted(starts)
    tool_event = detail["events"][3]
    assert tool_event["attributes"]["agentflight.tool.args"] == {"query": "cats"}
    assert tool_event["attributes"]["agentflight.tool.result"] == ["result for cats"]
    assert all(e["duration_ms"] is not None for e in detail["events"])


def test_failed_run_reports_error(recorded: TestClient) -> None:
    @agentflight.tool
    def broken() -> None:
        raise KeyError("missing")

    with pytest.raises(KeyError), agentflight.run("fails") as r:
        broken()
    agentflight.flush()

    detail = recorded.get(f"/v1/runs/{r.run_id}").json()
    assert detail["status"] == "error"
    assert detail["errors"] == 1
    tool_event = next(e for e in detail["events"] if e["kind"] == "tool")
    assert tool_event["status"] == "error"
    assert "KeyError" in tool_event["status_message"]
    assert recorded.get("/v1/runs", params={"status": "error"}).json()["total"] == 1
    assert recorded.get("/v1/runs", params={"status": "ok"}).json()["total"] == 0


def test_children_before_root_creates_running_run(client: TestClient) -> None:
    child = span(T, "1" * 16, parent_span_id="2" * 16, attributes={"agentflight.kind": "tool"})
    assert client.post("/v1/spans", json={"spans": [child]}).status_code == 202
    assert client.get(f"/v1/runs/{T}").json()["status"] == "running"

    root = span(
        T,
        "2" * 16,
        start_time_ns=0,
        end_time_ns=5,
        status="ok",
        attributes={"agentflight.kind": "run", "agentflight.run.name": "late"},
    )
    client.post("/v1/spans", json={"spans": [root]})
    detail = client.get(f"/v1/runs/{T}").json()
    assert (detail["status"], detail["name"], detail["event_count"]) == ("ok", "late", 2)


def test_ingest_is_idempotent(client: TestClient) -> None:
    payload = {"spans": [span(T, "1" * 16)]}
    client.post("/v1/spans", json=payload)
    client.post("/v1/spans", json=payload)
    assert client.get(f"/v1/runs/{T}").json()["event_count"] == 1


def test_rejects_malformed_ids(client: TestClient) -> None:
    bad = span("xyz", "1" * 16)
    assert client.post("/v1/spans", json={"spans": [bad]}).status_code == 422


def test_unknown_run_404(client: TestClient) -> None:
    assert client.get(f"/v1/runs/{'b' * 32}").status_code == 404


def test_pagination(client: TestClient) -> None:
    for i in range(3):
        trace = f"{i:032x}"
        client.post("/v1/spans", json={"spans": [span(trace, "1" * 16, start_time_ns=i)]})
    page = client.get("/v1/runs", params={"limit": 2}).json()
    assert page["total"] == 3
    assert [r["id"] for r in page["runs"]] == [f"{2:032x}", f"{1:032x}"]


def test_exporter_reports_failure_when_server_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    exporter = AgentFlightExporter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    agentflight.configure(exporter=exporter, batch=False)
    with agentflight.run("offline"):
        pass  # must not raise even though export fails
    assert exporter.export([]) is SpanExportResult.FAILURE
    agentflight.shutdown()
