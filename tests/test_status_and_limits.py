"""Run status (unknown, abandoned at read time) and ingest limits."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from agentflight.server import create_app
from agentflight.server.models import Run
from agentflight.server.settings import Settings

T = "a" * 32
MINUTE_NS = 60 * 1_000_000_000


def span(trace: str = T, sid: str = "1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trace_id": trace,
        "span_id": sid * 16,
        "name": "s",
        "start_time_ns": 1,
        "end_time_ns": 2,
        "attributes": {},
    }
    base.update(kw)
    return base


def root(trace: str = T, **kw: Any) -> dict[str, Any]:
    attrs = {"agentflight.kind": "run", **kw.pop("attributes", {})}
    return span(trace, "0", attributes=attrs, **kw)


def app_client(engine: Engine, **settings: Any) -> TestClient:
    return TestClient(create_app(engine, Settings(auth_disabled=True, **settings)))


def test_root_without_status_is_unknown(client: TestClient) -> None:
    client.post("/v1/spans", json={"spans": [root()]})
    assert client.get(f"/v1/runs/{T}").json()["status"] == "unknown"
    assert client.get("/v1/runs", params={"status": "unknown"}).json()["total"] == 1
    assert client.get("/v1/runs", params={"status": "ok"}).json()["total"] == 0


def _age_run(engine: Engine, minutes: float, trace: str = T) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(Run)
            .where(Run.id == trace)
            .values(last_seen_ns=time.time_ns() - int(minutes * MINUTE_NS))
        )


@pytest.fixture
def stale_setup(engine: Engine) -> Iterator[Engine]:
    c = app_client(engine)
    # T: running, quiet for 90 min. B: running, quiet for 5 min. C: finished long ago.
    c.post("/v1/spans", json={"spans": [span(T, parent_span_id="0" * 16)]})
    c.post("/v1/spans", json={"spans": [span("b" * 32, parent_span_id="0" * 16)]})
    c.post("/v1/spans", json={"spans": [root("c" * 32, status="ok")]})
    _age_run(engine, 90)
    _age_run(engine, 5, "b" * 32)
    _age_run(engine, 500, "c" * 32)
    yield engine


def statuses(c: TestClient) -> dict[str, str]:
    return {r["id"][0]: r["status"] for r in c.get("/v1/runs").json()["runs"]}


def test_abandoned_is_computed_at_read_time(stale_setup: Engine) -> None:
    c = app_client(stale_setup, stale_after_minutes=60)
    assert statuses(c) == {"a": "abandoned", "b": "running", "c": "ok"}
    assert c.get(f"/v1/runs/{T}").json()["status"] == "abandoned"
    assert c.get("/v1/runs", params={"status": "abandoned"}).json()["runs"][0]["id"] == T
    running = c.get("/v1/runs", params={"status": "running"}).json()
    assert [r["id"] for r in running["runs"]] == ["b" * 32]
    assert running["total"] == 1


def test_changing_the_threshold_needs_no_data_fix(stale_setup: Engine) -> None:
    assert statuses(app_client(stale_setup, stale_after_minutes=120))["a"] == "running"
    assert statuses(app_client(stale_setup, stale_after_minutes=1))["b"] == "abandoned"
    off = app_client(stale_setup, stale_after_minutes=0)
    assert statuses(off) == {"a": "running", "b": "running", "c": "ok"}
    assert off.get("/v1/runs", params={"status": "abandoned"}).json()["total"] == 0
    assert off.get("/v1/runs", params={"status": "running"}).json()["total"] == 2


def test_new_span_revives_an_abandoned_run(stale_setup: Engine) -> None:
    c = app_client(stale_setup, stale_after_minutes=60)
    c.post("/v1/spans", json={"spans": [span(T, "2", parent_span_id="0" * 16)]})
    assert statuses(c)["a"] == "running"
    c.post("/v1/spans", json={"spans": [root(T, status="error")]})
    assert statuses(c)["a"] == "error"


def test_last_seen_is_server_time(client: TestClient) -> None:
    before = time.time_ns()
    client.post("/v1/spans", json={"spans": [root()]})
    assert before <= client.get(f"/v1/runs/{T}").json()["last_seen_ns"] <= time.time_ns()


# --- limits -----------------------------------------------------------------


def _nothing_stored(c: TestClient) -> bool:
    return bool(c.get("/v1/runs").json()["total"] == 0)


def test_body_size_limit_with_content_length(engine: Engine) -> None:
    c = app_client(engine, max_body_bytes=2_000)
    ok = {"spans": [root()]}
    assert c.post("/v1/spans", json=ok).status_code == 202
    big = {"spans": [root("b" * 32, attributes={"agentflight.kind": "run", "x": "y" * 3_000})]}
    resp = c.post("/v1/spans", json=big)
    assert resp.status_code == 413
    assert "2000 bytes" in resp.json()["detail"]
    assert c.get("/v1/runs").json()["total"] == 1


def test_body_size_limit_for_chunked_bodies(engine: Engine) -> None:
    c = app_client(engine, max_body_bytes=2_000)
    body = json.dumps({"spans": [root(attributes={"x": "y" * 3_000})]}).encode()

    def chunks() -> Iterator[bytes]:
        for i in range(0, len(body), 500):
            yield body[i : i + 500]

    resp = c.post("/v1/spans", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert _nothing_stored(c)


def test_spans_per_batch_limit(engine: Engine) -> None:
    c = app_client(engine, max_spans_per_batch=3)
    spans = [span(sid=f"{i}") for i in range(1, 5)]
    resp = c.post("/v1/spans", json={"spans": spans})
    assert resp.status_code == 413
    assert "the limit is 3" in resp.json()["detail"]
    assert _nothing_stored(c)
    assert c.post("/v1/spans", json={"spans": spans[:3]}).status_code == 202


@pytest.mark.parametrize(
    "bad",
    [
        {"attributes": {"x": "y" * 101}},
        {"attributes": {"x": ["y" * 60, "z" * 60]}},
        {"attributes": {"k" * 256: 1}},
        {"attributes": {f"k{i}": i for i in range(6)}},
        {"status_message": "m" * 101},
        {"events": [{"name": "e", "time_ns": 1, "attributes": {}}] * 6},
        {"events": [{"name": "e", "time_ns": 1, "attributes": {"stack": "s" * 101}}]},
    ],
)
def test_span_content_limits(engine: Engine, bad: dict[str, Any]) -> None:
    c = app_client(
        engine, max_attribute_chars=100, max_attributes_per_span=5, max_events_per_span=5
    )
    resp = c.post("/v1/spans", json={"spans": [root(), span(sid="2", **bad)]})
    assert resp.status_code == 413
    assert "span 1" in resp.json()["detail"]
    assert _nothing_stored(c)


def test_default_limits_accept_a_default_sdk_sized_batch(client: TestClient) -> None:
    # 512 spans (the SDK's default export batch) with a 32,000-character payload each
    # (the SDK's default max_attr_chars).
    payload = "p" * 32_000
    spans = [root()] + [
        span(sid="0", span_id=f"{i:016x}", attributes={"agentflight.tool.result": payload})
        for i in range(1, 512)
    ]
    assert client.post("/v1/spans", json={"spans": spans}).status_code == 202


def test_long_names_are_rejected_not_a_database_error(client: TestClient) -> None:
    assert client.post("/v1/spans", json={"spans": [span(name="n" * 256)]}).status_code == 422
    long_run = root(attributes={"agentflight.kind": "run", "agentflight.run.name": "r" * 300})
    assert client.post("/v1/spans", json={"spans": [long_run]}).status_code == 202
    assert client.get(f"/v1/runs/{T}").json()["name"] == "r" * 255
    weird = span("b" * 32, attributes={"agentflight.kind": "k" * 40})
    assert client.post("/v1/spans", json={"spans": [weird]}).status_code == 202


def test_limit_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFLIGHT_AUTH_DISABLED", "true")
    monkeypatch.setenv("AGENTFLIGHT_MAX_BODY_BYTES", "1234")
    monkeypatch.setenv("AGENTFLIGHT_MAX_SPANS_PER_BATCH", "7")
    monkeypatch.setenv("AGENTFLIGHT_STALE_AFTER_MINUTES", "0")
    s = Settings.from_env()
    s.validate()
    assert (s.max_body_bytes, s.max_spans_per_batch, s.stale_after_minutes) == (1234, 7, 0)
    assert s.abandoned_before_ns(10) is None


@pytest.mark.parametrize(
    "kw", [{"max_body_bytes": 0}, {"max_spans_per_batch": -1}, {"stale_after_minutes": -1}]
)
def test_bad_limits_stop_startup(kw: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError):
        Settings(auth_disabled=True, **kw).validate()
