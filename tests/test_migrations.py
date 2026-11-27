from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from agentflight.server.db import make_engine
from agentflight.server.models import Base

ROOT = Path(__file__).resolve().parents[1]


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def _drop_everything(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'm.db'}"
        return
    url = os.environ.get("AGENTFLIGHT_TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("set AGENTFLIGHT_TEST_DATABASE_URL to a Postgres URL")
    engine = make_engine(url)
    _drop_everything(engine)
    yield url
    _drop_everything(engine)
    engine.dispose()


def test_alembic_upgrade_matches_models(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "head")

    engine = make_engine(db_url)
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        migrated = {c["name"] for c in insp.get_columns(table.name)}
        assert migrated == {c.name for c in table.columns}, table.name
        indexes = {i["name"] for i in insp.get_indexes(table.name)}
        assert indexes == {i.name for i in table.indexes}, table.name
    engine.dispose()

    command.downgrade(cfg, "base")
    engine = make_engine(db_url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def _insert(conn: object, sql: str, **params: object) -> None:
    conn.execute(text(sql), params)  # type: ignore[attr-defined]


EVENT_SQL = (
    "INSERT INTO events (span_id, run_id, parent_span_id, kind, name, status, "
    "start_time_ns, end_time_ns, attributes, span_events) "
    "VALUES (:sid, :rid, NULL, :kind, :kind, :status, :start, :end, :attrs, '[]')"
)


def test_0002_backfills_event_columns_and_run_totals(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "0001")
    run_a, run_b = "a" * 32, "b" * 32
    events = [
        (run_a, "1", "run", "error", 0, 100, {"agentflight.failure.id": "f1"}),
        (
            run_a,
            "2",
            "llm",
            "ok",
            10,
            20,
            {
                "agentflight.llm.model": "m-1",
                "agentflight.llm.tokens_in": 10,
                "agentflight.llm.tokens_out": 4,
                "agentflight.llm.cost_usd": 0.5,
            },
        ),
        (
            run_a,
            "3",
            "llm",
            "ok",
            20,
            30,
            {
                "agentflight.llm.model": "m-2",
                "agentflight.llm.tokens_in": "bad",
                "agentflight.llm.tokens_out": 6,
            },
        ),
        (
            run_a,
            "4",
            "tool",
            "error",
            30,
            None,
            {
                "agentflight.tool.name": "search",
                "agentflight.failure.id": "f1",
            },
        ),
        (run_a, "5", "tool", "error", 40, 45, {"agentflight.tool.name": "fetch"}),
        (run_b, "6", "run", "ok", 5, 6, {}),
    ]
    engine = make_engine(db_url)
    with engine.begin() as conn:
        for rid in (run_a, run_b):
            _insert(
                conn,
                "INSERT INTO runs (id, name, status, metadata) VALUES (:id, 'r', 'ok', '{}')",
                id=rid,
            )
        for rid, sid, kind, status, start, end, attrs in events:
            _insert(
                conn,
                EVENT_SQL,
                sid=sid * 16,
                rid=rid,
                kind=kind,
                status=status,
                start=start,
                end=end,
                attrs=json.dumps(attrs),
            )
    engine.dispose()

    command.upgrade(cfg, "0002")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        cols = {
            row[0]: tuple(row[1:])
            for row in conn.execute(
                text(
                    "SELECT span_id, model, tool_name, tokens_in, tokens_out, cost_usd, "
                    "failure_id FROM events"
                )
            )
        }
        runs = {
            row[0]: tuple(row[1:])
            for row in conn.execute(
                text(
                    "SELECT id, event_count, llm_calls, tool_calls, errors, error_events, "
                    "tokens_in, tokens_out, cost_usd, last_seen_ns FROM runs"
                )
            )
        }
    engine.dispose()
    assert cols["2" * 16] == ("m-1", None, 10, 4, 0.5, None)
    assert cols["3" * 16] == ("m-2", None, None, 6, None, None)
    assert cols["4" * 16] == (None, "search", None, None, None, "f1")
    assert cols["1" * 16] == (None, None, None, None, None, "f1")
    # errors: f1 (run + tool) and the tool without an id
    assert runs[run_a] == (5, 2, 2, 2, 3, 10, 10, 0.5, 100)
    assert runs[run_b] == (1, 0, 0, 0, 0, 0, 0, None, 6)

    command.downgrade(cfg, "0001")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == len(events)
        names = {c["name"] for c in inspect(conn).get_columns("runs")}
    engine.dispose()
    assert "event_count" not in names
    command.downgrade(cfg, "base")
