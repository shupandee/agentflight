from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from agentflight.server.db import make_engine
from agentflight.server.models import Base

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_matches_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")

    insp = inspect(make_engine(url))
    for table in Base.metadata.sorted_tables:
        migrated = {c["name"] for c in insp.get_columns(table.name)}
        assert migrated == {c.name for c in table.columns}, table.name

    command.downgrade(cfg, "base")
    assert set(inspect(make_engine(url)).get_table_names()) <= {"alembic_version"}
