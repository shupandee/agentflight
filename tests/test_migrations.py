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
    cfg.set_main_option("script_location", "agentflight.server:migrations")
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


def test_0003_marks_runs_without_a_root_status_unknown(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "0002")
    engine = make_engine(db_url)
    rows = [
        ("a", "ok", "unset"),
        ("b", "ok", "ok"),
        ("c", "error", "error"),
        ("d", "running", None),
    ]
    with engine.begin() as conn:
        for rid, status, root_status in rows:
            _insert(
                conn,
                "INSERT INTO runs (id, name, status, root_span_id, metadata) "
                "VALUES (:id, 'r', :status, :root, '{}')",
                id=rid * 32,
                status=status,
                root=(rid * 16) if root_status else None,
            )
            if root_status:
                _insert(
                    conn,
                    EVENT_SQL,
                    sid=rid * 16,
                    rid=rid * 32,
                    kind="run",
                    status=root_status,
                    start=1,
                    end=2,
                    attrs="{}",
                )
    engine.dispose()

    def statuses() -> dict[str, str]:
        e = make_engine(db_url)
        with e.connect() as conn:
            out = {r[0][0]: r[1] for r in conn.execute(text("SELECT id, status FROM runs"))}
        e.dispose()
        return out

    command.upgrade(cfg, "0003")
    assert statuses() == {"a": "unknown", "b": "ok", "c": "error", "d": "running"}
    command.downgrade(cfg, "0002")
    assert statuses() == {"a": "ok", "b": "ok", "c": "error", "d": "running"}
    command.downgrade(cfg, "base")


def test_sqlite_table_rebuild_keeps_existing_events(tmp_path: Path) -> None:
    """Regression: SQLite batch migrations rebuild tables (copy, drop, rename).

    With foreign keys on, dropping the old ``runs`` table cascade-deleted every event.
    Migrate a database that already holds events through every rebuild and check that
    all of them survive, and that the cascade still works for the app afterwards.
    """
    url = f"sqlite:///{tmp_path / 'rebuild.db'}"
    cfg = _config(url)
    command.upgrade(cfg, "0001")
    engine = make_engine(url)  # the app's engine: foreign keys on
    with engine.begin() as conn:
        for r in "ab":
            _insert(
                conn,
                "INSERT INTO runs (id, name, status, metadata) VALUES (:id, 'r', 'ok', '{}')",
                id=r * 32,
            )
            for i in range(1, 4):
                _insert(
                    conn,
                    EVENT_SQL,
                    sid=f"{r}{i}" * 8,
                    rid=r * 32,
                    kind="llm",
                    status="ok",
                    start=i,
                    end=i + 1,
                    attrs="{}",
                )
    engine.dispose()

    def snapshot() -> tuple[list[tuple[str, str]], list[str], str]:
        e = make_engine(url)
        with e.connect() as conn:
            events = [
                (r[0], r[1])
                for r in conn.execute(text("SELECT span_id, run_id FROM events ORDER BY 1"))
            ]
            runs = [r[0] for r in conn.execute(text("SELECT id FROM runs ORDER BY 1"))]
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'")
            ).scalar_one()
        e.dispose()
        return events, runs, ddl

    before_events, before_runs, _ = snapshot()
    assert len(before_events) == 6

    command.upgrade(cfg, "head")
    events, runs, ddl_head = snapshot()
    assert (events, runs) == (before_events, before_runs)

    command.downgrade(cfg, "0001")  # drops columns: rebuilds runs and events on SQLite
    events, runs, ddl_down = snapshot()
    assert ddl_down != ddl_head  # the table really was rebuilt
    assert (events, runs) == (before_events, before_runs)

    command.upgrade(cfg, "head")
    events, runs, _ = snapshot()
    assert (events, runs) == (before_events, before_runs)

    # Foreign keys still hold: deleting a run through the app's engine removes its events.
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": "a" * 32})
        left = conn.execute(text("SELECT DISTINCT run_id FROM events")).scalars().all()
    engine.dispose()
    assert left == ["b" * 32]


def test_0004_adds_dropped_spans_with_zero_for_existing_runs(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "0003")
    engine = make_engine(db_url)
    with engine.begin() as conn:
        _insert(
            conn,
            "INSERT INTO runs (id, name, status, metadata) VALUES (:id, 'r', 'ok', '{}')",
            id="a" * 32,
        )
    engine.dispose()
    command.upgrade(cfg, "0004")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT dropped_spans FROM runs")).scalar_one() == 0
    engine.dispose()
    command.downgrade(cfg, "0003")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        names = {c["name"] for c in inspect(conn).get_columns("runs")}
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1
    engine.dispose()
    assert "dropped_spans" not in names
    command.downgrade(cfg, "base")


def test_0005_adds_parent_run_columns_null_for_existing_runs(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "0004")
    engine = make_engine(db_url)
    with engine.begin() as conn:
        _insert(
            conn,
            "INSERT INTO runs (id, name, status, metadata) VALUES (:id, 'r', 'ok', '{}')",
            id="a" * 32,
        )
    engine.dispose()
    command.upgrade(cfg, "0005")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT parent_run_id, parent_span_id FROM runs")).one()
        assert tuple(row) == (None, None)
        assert "ix_runs_parent_run_id" in {i["name"] for i in inspect(conn).get_indexes("runs")}
    engine.dispose()
    command.downgrade(cfg, "0004")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        names = {c["name"] for c in inspect(conn).get_columns("runs")}
        indexes = {i["name"] for i in inspect(conn).get_indexes("runs")}
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1
    engine.dispose()
    assert not {"parent_run_id", "parent_span_id"} & names
    assert "ix_runs_parent_run_id" not in indexes
    command.downgrade(cfg, "base")


def test_migrate_command_uses_the_packaged_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentflight.server import cli

    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("AGENTFLIGHT_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)  # no alembic.ini or source checkout needed
    assert cli.main(["migrate"]) == 0
    engine = make_engine(url)
    try:
        assert {"runs", "events", "alembic_version"} <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert head.startswith("0005")
    finally:
        engine.dispose()
