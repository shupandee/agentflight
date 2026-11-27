"""The server stores the SDK's dropped-span count and the viewer warns about it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from agentflight import attributes as A
from agentflight.server import create_app
from agentflight.server.settings import Settings

T = "cd" * 16
WARNING = "incomplete recording: "


@pytest.fixture
def c(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)


def root(dropped: Any = None, trace: str = T) -> dict[str, Any]:
    attrs: dict[str, Any] = {A.KIND: A.RUN, A.RUN_NAME: "bursty"}
    if dropped is not None:
        attrs[A.RUN_DROPPED_SPANS] = dropped
    return {
        "trace_id": trace,
        "span_id": "1" * 16,
        "name": "run:bursty",
        "start_time_ns": 1,
        "end_time_ns": 2,
        "status": "ok",
        "attributes": attrs,
    }


def test_dropped_count_is_stored_and_returned(c: TestClient) -> None:
    assert c.post("/v1/spans", json={"spans": [root(7)]}).status_code == 202
    assert c.get(f"/v1/runs/{T}").json()["dropped_spans"] == 7
    assert c.get("/v1/runs").json()["runs"][0]["dropped_spans"] == 7


def test_runs_without_drops_report_zero_and_no_warning(c: TestClient) -> None:
    c.post("/v1/spans", json={"spans": [root()]})
    assert c.get(f"/v1/runs/{T}").json()["dropped_spans"] == 0
    assert WARNING not in c.get("/ui").text
    assert WARNING not in c.get(f"/ui/runs/{T}").text


@pytest.mark.parametrize(
    ("value", "stored"),
    [("lots", 0), (-3, 0), (True, 0), ("<script>alert(1)</script>", 0), (10**12, 2**31 - 1)],
)
def test_malformed_counts_from_other_senders(c: TestClient, value: Any, stored: int) -> None:
    assert c.post("/v1/spans", json={"spans": [root(value)]}).status_code == 202
    assert c.get(f"/v1/runs/{T}").json()["dropped_spans"] == stored
    page = c.get(f"/ui/runs/{T}").text
    assert "<script>alert(1)</script>" not in page


def test_run_list_and_run_page_warn(c: TestClient) -> None:
    c.post("/v1/spans", json={"spans": [root(1234), root(1, trace="ef" * 16)]})
    listing = c.get("/ui").text
    assert "incomplete recording: 1,234 spans dropped" in listing
    assert "incomplete recording: 1 span dropped" in listing
    page = c.get(f"/ui/runs/{T}")
    assert 'class="incomplete" role="alert"' in page.text
    assert "incomplete recording: 1,234 spans dropped" in page.text
    assert "style=" not in page.text  # styled by class; the CSP is unchanged
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    table = c.get("/ui/runs", headers={"HX-Request": "true"}).text  # htmx swap too
    assert "incomplete recording: 1,234 spans dropped" in table


def test_sdk_drops_show_up_in_the_viewer_end_to_end(c: TestClient) -> None:
    import threading
    from collections.abc import Sequence

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    import agentflight
    from agentflight.exporter import AgentFlightExporter

    real = AgentFlightExporter("http://testserver", client=c)
    release = threading.Event()

    class Held(SpanExporter):  # holds the first batch so the queue fills
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            release.wait(10)
            return real.export(spans)

    rec = agentflight.configure(exporter=Held(), batch=True, max_queue_size=40)
    with agentflight.run("bursty") as r:
        for i in range(200):
            agentflight.decision(f"d{i}", i)
    release.set()
    assert agentflight.flush(timeout_millis=10_000)
    dropped = rec.dropped_spans
    agentflight.shutdown()
    assert dropped > 0
    run = c.get(f"/v1/runs/{r.run_id}").json()
    assert run["dropped_spans"] == dropped
    assert run["event_count"] == 201 - dropped
    assert f"incomplete recording: {dropped:,} spans dropped" in c.get("/ui").text
