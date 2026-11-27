from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from agentflight.server import create_app
from agentflight.server.settings import Permission, Settings, parse_api_keys
from tests.conftest import TEST_KEY

INGEST_KEY = "ingest-key-0123456789abcdef0123456789"
READ_KEY = "read-key-0123456789abcdef0123456789"
READ_DELETE_KEY = "read-delete-key-0123456789abcdef0123456789"
RUN_ID = "e" * 32
SPAN = {
    "trace_id": RUN_ID,
    "span_id": "1" * 16,
    "name": "s",
    "start_time_ns": 1,
    "end_time_ns": 2,
    "status": "ok",
    "attributes": {"agentflight.kind": "run"},
}


@pytest.fixture
def anon(engine: Engine) -> TestClient:
    keys = parse_api_keys(f"{INGEST_KEY}:ingest,{READ_KEY}:read,{READ_DELETE_KEY}:read+delete")
    return TestClient(create_app(engine, Settings(api_key=TEST_KEY, scoped_keys=keys)))


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_parse_api_keys() -> None:
    keys = dict(parse_api_keys(" a:ingest , b:READ+delete,, "))
    assert keys == {
        "a": frozenset({Permission.INGEST}),
        "b": frozenset({Permission.READ, Permission.DELETE}),
    }
    assert parse_api_keys(None) == ()
    assert parse_api_keys("  ") == ()


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("secretvalue", "expected <key>:<permissions>"),
        (":read", "expected <key>:<permissions>"),
        ("secretvalue:admin", "unknown permission 'admin'"),
        ("secretvalue:", "unknown permission ''"),
        ("secretvalue:read,secretvalue:ingest", "duplicate key"),
    ],
)
def test_parse_api_keys_errors_never_echo_the_key(raw: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match) as exc:
        parse_api_keys(raw)
    assert "secretvalue" not in str(exc.value)


def test_ingest_only_key_cannot_read_or_delete(anon: TestClient) -> None:
    h = _auth(INGEST_KEY)
    assert anon.post("/v1/spans", json={"spans": [SPAN]}, headers=h).status_code == 202
    assert anon.get("/v1/runs", headers=h).status_code == 403
    assert anon.get(f"/v1/runs/{RUN_ID}", headers=h).status_code == 403
    assert anon.delete(f"/v1/runs/{RUN_ID}", headers=h).status_code == 403


def test_read_only_key_cannot_ingest_or_delete(anon: TestClient) -> None:
    anon.post("/v1/spans", json={"spans": [SPAN]}, headers=_auth(INGEST_KEY))
    h = _auth(READ_KEY)
    assert anon.get("/v1/runs", headers=h).json()["total"] == 1
    assert anon.get(f"/v1/runs/{RUN_ID}", headers=h).status_code == 200
    assert anon.post("/v1/spans", json={"spans": [SPAN]}, headers=h).status_code == 403
    assert anon.delete(f"/v1/runs/{RUN_ID}", headers=h).status_code == 403


def test_read_delete_key_and_full_key(anon: TestClient) -> None:
    anon.post("/v1/spans", json={"spans": [SPAN]}, headers=_auth(TEST_KEY))
    assert anon.delete(f"/v1/runs/{RUN_ID}", headers=_auth(READ_DELETE_KEY)).status_code == 204
    assert anon.get("/v1/runs", headers=_auth(TEST_KEY)).json()["total"] == 0


def test_scoped_keys_alone_are_enough_to_start(engine: Engine) -> None:
    app = create_app(engine, Settings(scoped_keys=parse_api_keys(f"{READ_KEY}:read")))
    assert TestClient(app).get("/v1/runs", headers=_auth(READ_KEY)).status_code == 200


def test_scoped_keys_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFLIGHT_API_KEY", raising=False)
    monkeypatch.setenv("AGENTFLIGHT_API_KEYS", f"{INGEST_KEY}:ingest")
    s = Settings.from_env()
    s.validate()
    assert s.api_keys == {INGEST_KEY: frozenset({Permission.INGEST})}
    assert INGEST_KEY not in repr(s)


def test_full_key_repeated_as_scoped_key_is_rejected() -> None:
    s = Settings(api_key=TEST_KEY, scoped_keys=((TEST_KEY, frozenset({Permission.READ})),))
    with pytest.raises(RuntimeError, match="repeats"):
        s.validate()


def test_scoped_keys_are_scrubbed_from_logs(
    anon: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("agentflight").warning("leak %s %s", INGEST_KEY, READ_KEY)
    assert INGEST_KEY not in caplog.text
    assert READ_KEY not in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "setting"),
    [
        ({"api_key": "short-secret-key"}, "AGENTFLIGHT_API_KEY"),
        ({"api_key": "x" * 31}, "AGENTFLIGHT_API_KEY"),
        (
            {"scoped_keys": parse_api_keys(f"{READ_KEY}:read,short-secret-key:ingest")},
            "AGENTFLIGHT_API_KEYS key #2",
        ),
    ],
)
def test_short_keys_stop_startup_without_echoing_them(
    engine: Engine, kwargs: dict[str, Any], setting: str
) -> None:
    with pytest.raises(RuntimeError) as err:
        create_app(engine, Settings(**kwargs))
    message = str(err.value)
    assert setting in message and "32 characters" in message
    assert "short-secret-key" not in message and "x" * 31 not in message


def test_keys_of_exactly_32_characters_are_accepted(engine: Engine) -> None:
    create_app(engine, Settings(api_key="a" * 32, scoped_keys=parse_api_keys("b" * 32 + ":read")))


def test_short_key_from_env_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFLIGHT_API_KEYS", "tiny:read")
    monkeypatch.delenv("AGENTFLIGHT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="shorter than 32") as err:
        Settings.from_env().validate()
    assert "tiny" not in str(err.value)
