from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from agentflight.server import service
from agentflight.server.cli import NS_PER_DAY, main
from agentflight.server.db import session_factory
from agentflight.server.models import Event, Run
from agentflight.server.schemas import SpanIn
from agentflight.server.settings import Settings


def _spans(trace: str, start_ns: int, *, finished: bool = True) -> list[SpanIn]:
    end = start_ns + 10 if finished else None
    root = {"agentflight.kind": "run", "agentflight.run.name": trace[:4]}
    return [
        SpanIn(
            trace_id=trace,
            span_id="1" * 16,
            name="run",
            start_time_ns=start_ns,
            end_time_ns=end,
            attributes=root if finished else {"agentflight.kind": "tool"},
        ),
        SpanIn(
            trace_id=trace,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            name="tool",
            start_time_ns=start_ns + 1,
            end_time_ns=start_ns + 2,
            attributes={"agentflight.kind": "tool"},
        ),
    ]


def _counts(engine: Engine) -> tuple[int, int]:
    with session_factory(engine)() as s:
        return (
            s.scalar(select(func.count()).select_from(Run)) or 0,
            s.scalar(select(func.count()).select_from(Event)) or 0,
        )


def test_delete_run_removes_run_and_events(client: TestClient) -> None:
    trace = "e" * 32
    spans = [s.model_dump() for s in _spans(trace, 1)]
    client.post("/v1/spans", json={"spans": spans})
    other = "f" * 32
    client.post("/v1/spans", json={"spans": [s.model_dump() for s in _spans(other, 1)]})

    assert client.delete(f"/v1/runs/{trace}").status_code == 204
    assert client.get(f"/v1/runs/{trace}").status_code == 404
    assert client.get(f"/v1/runs/{other}").json()["event_count"] == 2
    assert client.delete(f"/v1/runs/{trace}").status_code == 404


def test_cleanup_deletes_only_expired(engine: Engine) -> None:
    now = time.time_ns()
    with session_factory(engine)() as s:
        service.ingest_spans(s, _spans("1" * 32, now - 40 * NS_PER_DAY))  # old, finished
        service.ingest_spans(s, _spans("2" * 32, now - 40 * NS_PER_DAY, finished=False))
        service.ingest_spans(s, _spans("3" * 32, now - 1 * NS_PER_DAY))  # recent
    assert _counts(engine) == (3, 6)

    with session_factory(engine)() as s:
        cutoff = now - 30 * NS_PER_DAY
        assert service.cleanup_expired(s, cutoff_ns=cutoff, dry_run=True) == 2
        assert _counts(engine) == (3, 6)
        assert service.cleanup_expired(s, cutoff_ns=cutoff) == 2
    assert _counts(engine) == (1, 2)


@pytest.fixture
def db_env(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from agentflight.server.db import make_engine
    from agentflight.server.models import Base

    url = f"sqlite:///{tmp_path / 'r.db'}"
    monkeypatch.setenv("AGENTFLIGHT_DATABASE_URL", url)
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    now = time.time_ns()
    with session_factory(engine)() as s:
        service.ingest_spans(s, _spans("1" * 32, now - 40 * NS_PER_DAY))
        service.ingest_spans(s, _spans("3" * 32, now - 5 * NS_PER_DAY))
    yield engine
    engine.dispose()


def test_cli_cleanup_uses_configured_days(
    db_env: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cleanup", "--dry-run"]) == 0
    assert "would delete 1 runs older than 30 days" in capsys.readouterr().out
    monkeypatch.setenv("AGENTFLIGHT_RETENTION_DAYS", "2")
    assert main(["cleanup"]) == 0
    assert "deleted 2 runs older than 2 days" in capsys.readouterr().out
    assert _counts(db_env) == (0, 0)


def test_cli_cleanup_respects_disabled(
    db_env: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTFLIGHT_RETENTION_ENABLED", "false")
    assert main(["cleanup", "--days", "1"]) == 0
    assert "retention disabled" in capsys.readouterr().out
    assert _counts(db_env) == (2, 4)


def test_settings_defaults_and_validation() -> None:
    s = Settings(api_key="k")
    assert (s.retention_enabled, s.retention_days, s.cleanup_interval_hours) == (True, 30, 24)
    with pytest.raises(RuntimeError):
        Settings(api_key="k" * 32, retention_days=0).validate()
