"""Viewer pages: built on the service layer, paged, and escaping everything recorded."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

import agentflight
from agentflight.exporter import AgentFlightExporter
from agentflight.server import create_app, service
from agentflight.server.models import Run
from agentflight.server.settings import Settings
from agentflight.server.viewer import routes

XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'
ESCAPED = "&lt;script&gt;alert("


@pytest.fixture
def ui(engine: Engine) -> Iterator[TestClient]:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    agentflight.configure(exporter=AgentFlightExporter("http://testserver", client=c), batch=False)
    yield c
    agentflight.shutdown()


@agentflight.tool
def lookup(query: str) -> str:
    if "fail" in query:
        raise LookupError(f"nothing for {query}")
    return f"result for {query}"


def record(name: str, query: str, **meta: Any) -> str:
    with agentflight.run(name, metadata=meta) as r:
        with agentflight.llm_call("model-a", [{"role": "user", "content": query}]) as call:
            call.record(f"answer {query}", tokens_in=12, tokens_out=3, cost_usd=0.001)
        try:
            lookup(query)
        except LookupError as exc:
            agentflight.error(exc)
    return r.run_id


def test_run_list_page_and_table(ui: TestClient) -> None:
    ok = record("weather", "paris", env="prod")
    bad = record("weather-fail", "fail now", env="dev")
    page = ui.get("/ui")
    assert page.status_code == 200
    assert f"/ui/runs/{ok}" in page.text and f"/ui/runs/{bad}" in page.text
    assert 'hx-get="/ui/runs"' in page.text
    table = ui.get("/ui/runs", params={"has_errors": "yes"}, headers={"HX-Request": "true"})
    assert f"/ui/runs/{bad}" in table.text and f"/ui/runs/{ok}" not in table.text
    assert "<html" not in table.text
    assert f"/ui/runs/{ok}" in ui.get("/ui", params={"meta": "env:prod"}).text
    assert f"/ui/runs/{bad}" not in ui.get("/ui", params={"meta": "env:prod"}).text
    # A plain visit to the table URL goes to the full page with the same filters.
    plain = ui.get("/ui/runs", params={"status": "ok"})
    assert (plain.status_code, plain.headers["location"]) == (303, "/ui?status=ok")


def test_bad_filters_show_messages_not_errors(ui: TestClient) -> None:
    resp = ui.get("/ui", params={"status": "nope", "meta": "x", "since": "tomorrow"})
    assert resp.status_code == 200
    assert "Unknown status" in resp.text
    assert "key:value" in resp.text
    assert "valid date and time" in resp.text


def test_time_filters_read_utc(ui: TestClient) -> None:
    run_id = record("t", "paris")
    assert f"/ui/runs/{run_id}" in ui.get("/ui", params={"since": "2000-01-01T00:00"}).text
    assert f"/ui/runs/{run_id}" not in ui.get("/ui", params={"until": "2000-01-01T00:00"}).text


def test_run_page_uses_paged_payload_free_mode(
    ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = record("weather", "paris-SECRET-PAYLOAD")
    calls: list[dict[str, Any]] = []
    real = service.get_run_tree_page

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "get_run_tree_page", spy)
    whole_run = service.get_run
    monkeypatch.setattr(service, "get_run", None)  # the viewer never loads a whole run
    page = ui.get(f"/ui/runs/{run_id}")
    monkeypatch.setattr(service, "get_run", whole_run)
    assert page.status_code == 200
    assert calls and all(c["limit"] == routes.EVENTS_PAGE for c in calls)
    # Payloads aren't on the page; they load per event.
    assert "answer paris-SECRET-PAYLOAD" not in page.text
    assert "model-a" in page.text and "<svg" in page.text
    llm_event = next(
        e
        for e in ui.get(f"/v1/runs/{run_id}", params={"payloads": "false"}).json()["events"]
        if e["kind"] == "llm"
    )
    panel = ui.get(
        f"/ui/runs/{run_id}/events/{llm_event['span_id']}", headers={"HX-Request": "true"}
    )
    assert "answer paris-SECRET-PAYLOAD" in panel.text
    assert "<html" not in panel.text
    full = ui.get(f"/ui/runs/{run_id}/events/{llm_event['span_id']}")
    assert "<html" in full.text and "answer paris-SECRET-PAYLOAD" in full.text


def test_failures_are_highlighted_with_stack_traces(ui: TestClient) -> None:
    run_id = record("weather-fail", "fail hard")
    page = ui.get(f"/ui/runs/{run_id}")
    assert 'class="k-tool err"' in page.text
    assert "nothing for fail hard" in page.text
    tool = next(e for e in ui.get(f"/v1/runs/{run_id}").json()["events"] if e["kind"] == "tool")
    panel = ui.get(f"/ui/runs/{run_id}/events/{tool['span_id']}", headers={"HX-Request": "1"})
    assert "Traceback (most recent call last)" in panel.text


def test_long_timelines_load_more(ui: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "EVENTS_PAGE", 2)
    with agentflight.run("many") as r:
        for i in range(5):
            agentflight.decision(f"d{i}", i)
    page = ui.get(f"/ui/runs/{r.run_id}")
    assert page.text.count('class="k-decision') + page.text.count('class="k-run') == 2
    assert f"/ui/runs/{r.run_id}/events?offset=2" in page.text
    more = ui.get(f"/ui/runs/{r.run_id}/events", params={"offset": 2})
    assert more.text.count("<tr class=") == 3  # two events and the next "load more" row
    assert f"/ui/runs/{r.run_id}/events?offset=4" in more.text
    last = ui.get(f"/ui/runs/{r.run_id}/events", params={"offset": 4})  # events 5 and 6 of 6
    assert last.text.count("<tr class=") == 2
    assert "Load more" not in last.text


def test_abandoned_runs_are_shown(engine: Engine, ui: TestClient) -> None:
    with pytest.raises(RuntimeError), agentflight.run("crashy"):
        raise RuntimeError("boom")
    # A child span arriving without its run span: a run still running.
    ui.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "trace_id": "e" * 32,
                    "span_id": "1" * 16,
                    "parent_span_id": "2" * 16,
                    "name": "stuck",
                    "start_time_ns": time.time_ns(),
                    "attributes": {"agentflight.kind": "tool"},
                }
            ]
        },
    )
    with engine.begin() as conn:
        conn.execute(update(Run).where(Run.id == "e" * 32).values(last_seen_ns=1))
    assert 'st-abandoned">abandoned' in ui.get("/ui").text
    assert 'st-abandoned">abandoned' in ui.get(f"/ui/runs/{'e' * 32}").text


def test_replay_runs_link_to_their_source(ui: TestClient) -> None:
    source = record("weather", "paris")
    recording = agentflight.Recording.from_run(ui.get(f"/v1/runs/{source}").json())
    with agentflight.replay(recording), agentflight.run("weather") as replayed:
        with agentflight.llm_call("model-a", [{"role": "user", "content": "paris"}]) as call:
            assert call.replayed
        lookup("paris")
    page = ui.get(f"/ui/runs/{replayed.run_id}")
    assert f'href="/ui/runs/{source}"' in page.text
    assert "replay of" in page.text


def test_not_found_pages(ui: TestClient) -> None:
    assert ui.get(f"/ui/runs/{'f' * 32}").status_code == 404
    assert ui.get(f"/ui/runs/{'f' * 32}/events/{'1' * 16}").status_code == 404
    assert ui.get(f"/ui/runs/{'f' * 32}/events").status_code == 404


def test_recorded_content_is_escaped_everywhere(ui: TestClient) -> None:
    with pytest.raises(LookupError), agentflight.run(XSS, metadata={XSS: XSS}) as r:
        with agentflight.llm_call(XSS, [{"role": "user", "content": XSS}]) as call:
            call.record(XSS, tokens_in=1, tokens_out=1)
        agentflight.decision(XSS, XSS, reason=XSS)
        lookup("fail " + XSS)
    events = ui.get(f"/v1/runs/{r.run_id}").json()["events"]
    pages = [
        ui.get("/ui"),
        ui.get("/ui/runs", headers={"HX-Request": "true"}),
        ui.get(f"/ui/runs/{r.run_id}"),
        ui.get(f"/ui/runs/{r.run_id}/events"),
    ]
    for e in events:
        pages.append(ui.get(f"/ui/runs/{r.run_id}/events/{e['span_id']}"))
        pages.append(
            ui.get(f"/ui/runs/{r.run_id}/events/{e['span_id']}", headers={"HX-Request": "1"})
        )
    for page in pages:
        assert page.status_code == 200, page.url
        assert "<script>alert" not in page.text, page.url
        assert "<img src=x" not in page.text, page.url
        assert "onerror=alert" not in page.text or "&lt;img" in page.text, page.url
    assert any(ESCAPED in p.text for p in pages)
    # Even recorded payloads that look like htmx attributes stay inert text.
    assert all(p.text.count("<script") <= 1 for p in pages)  # only the htmx <script src>


def test_viewer_needs_read_permission_only(engine: Engine) -> None:
    from agentflight.server.settings import parse_api_keys

    keys = parse_api_keys("viewer-key-0123456789abcdef0123456789:read")
    c = TestClient(create_app(engine, Settings(scoped_keys=keys)), follow_redirects=False)
    assert (
        c.post(
            "/ui/login",
            data={"key": "viewer-key-0123456789abcdef0123456789"},
            headers={"Origin": "http://testserver"},
        ).status_code
        == 303
    )
    assert c.get("/ui").status_code == 200
    for method in ("post", "put", "delete"):
        assert getattr(c, method)(f"/ui/runs/{'a' * 32}").status_code == 405


def test_htmx_is_vendored_and_served_locally(ui: TestClient) -> None:
    page = ui.get("/ui")
    assert '<script src="/ui/static/htmx-2.0.11.min.js" defer></script>' in page.text
    assert "https://" not in page.text.split("<body>")[0]
    js = ui.get("/ui/static/htmx-2.0.11.min.js")
    assert js.status_code == 200 and js.text.startswith("var htmx=")
    assert '"allowEval":false' in page.text and '"historyCacheSize":0' in page.text
