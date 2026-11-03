from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import agentflight
from agentflight.exporter import AgentFlightExporter
from agentflight.server import create_app
from agentflight.server.auth import REDACTED, redact_headers
from agentflight.server.settings import Settings
from tests.conftest import TEST_KEY

SPAN = {
    "trace_id": "d" * 32,
    "span_id": "1" * 16,
    "name": "s",
    "start_time_ns": 1,
    "end_time_ns": 2,
    "attributes": {"agentflight.kind": "run"},
}

PROTECTED = [
    ("post", "/v1/spans", {"json": {"spans": [SPAN]}}),
    ("get", "/v1/runs", {}),
    ("get", f"/v1/runs/{'d' * 32}", {}),
    ("delete", f"/v1/runs/{'d' * 32}", {}),
]


@pytest.fixture
def anon(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, Settings(api_key=TEST_KEY)))


@pytest.mark.parametrize(("method", "path", "kw"), PROTECTED)
def test_missing_key_is_401(anon: TestClient, method: str, path: str, kw: dict) -> None:
    resp = getattr(anon, method)(path, **kw)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TEST_KEY}"},
        {"Authorization": f"Bearer {TEST_KEY}x"},
        {"X-AgentFlight-Key": "wrong"},
        {"Authorization": "Bearer "},
    ],
)
def test_wrong_key_is_401(anon: TestClient, headers: dict[str, str]) -> None:
    resp = anon.get("/v1/runs", headers=headers)
    assert resp.status_code == 401
    assert TEST_KEY not in resp.text


@pytest.mark.parametrize(
    "headers", [{"Authorization": f"Bearer {TEST_KEY}"}, {"X-AgentFlight-Key": TEST_KEY}]
)
def test_valid_key_accepted(anon: TestClient, headers: dict[str, str]) -> None:
    assert anon.post("/v1/spans", json={"spans": [SPAN]}, headers=headers).status_code == 202
    assert anon.get("/v1/runs", headers=headers).json()["total"] == 1


def test_health_and_ready_need_no_key(anon: TestClient) -> None:
    assert anon.get("/health").status_code == 200
    assert anon.get("/ready").status_code == 200


def test_auth_required_by_default(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFLIGHT_API_KEY", raising=False)
    monkeypatch.delenv("AGENTFLIGHT_AUTH_DISABLED", raising=False)
    with pytest.raises(RuntimeError, match="AGENTFLIGHT_API_KEY"):
        create_app(engine)


def test_explicit_auth_disabled_for_local_dev(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTFLIGHT_API_KEY", raising=False)
    monkeypatch.setenv("AGENTFLIGHT_AUTH_DISABLED", "true")
    c = TestClient(create_app(engine))
    assert c.get("/v1/runs").status_code == 200


def test_settings_repr_hides_key() -> None:
    assert TEST_KEY not in repr(Settings(api_key=TEST_KEY))


def test_redact_headers() -> None:
    out = redact_headers({"Authorization": "Bearer s", "X-AgentFlight-Key": "s", "Accept": "a"})
    assert out == {"Authorization": REDACTED, "X-AgentFlight-Key": REDACTED, "Accept": "a"}


def test_key_never_logged(anon: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("uvicorn.access").info("GET /v1/runs Authorization: Bearer %s", TEST_KEY)
    anon.get("/v1/runs", headers={"Authorization": f"Bearer {TEST_KEY}"})
    anon.get("/v1/runs", headers={"Authorization": "Bearer wrong"})
    assert TEST_KEY not in caplog.text
    assert REDACTED in caplog.text


def test_sdk_sends_key_end_to_end(anon: TestClient) -> None:
    exporter = AgentFlightExporter("http://testserver", client=anon, api_key=TEST_KEY)
    agentflight.configure(exporter=exporter, batch=False)
    with agentflight.run("authed") as r:
        pass
    agentflight.shutdown()
    resp = anon.get(f"/v1/runs/{r.run_id}", headers={"Authorization": f"Bearer {TEST_KEY}"})
    assert resp.json()["name"] == "authed"


def test_sdk_reads_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(202)

    monkeypatch.setenv("AGENTFLIGHT_API_KEY", "env-key")
    rec = agentflight.configure("http://x", batch=False)
    exporter = rec.provider._active_span_processor._span_processors[0].span_exporter
    exporter._client = httpx.Client(transport=httpx.MockTransport(handler))
    with agentflight.run("r"):
        pass
    agentflight.shutdown()
    assert seen == ["Bearer env-key"]


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_auth_failure_never_breaks_agent(
    anon: TestClient, key: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    exporter = AgentFlightExporter("http://testserver", client=anon, api_key=key)
    agentflight.configure(exporter=exporter, batch=False)

    @agentflight.tool
    def add(a: int, b: int) -> int:
        return a + b

    with agentflight.run("rejected"):
        assert add(1, 2) == 3
        assert add(3, 4) == 7
    agentflight.shutdown()
    warnings = [r for r in caplog.records if "rejected credentials" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not per span
    assert "wrong-key" not in caplog.text
    assert repr(exporter) == "AgentFlightExporter(url='http://testserver/v1/spans')"
