"""Engine/session setup. Postgres in production, SQLite for tests and local hacking."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_URL = "sqlite:///./agentflight.db"


def database_url() -> str:
    return os.environ.get("AGENTFLIGHT_DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    if url.startswith("sqlite"):
        kwargs: dict[str, object] = {
            "connect_args": {"check_same_thread": False},
            # Database errors are logged; their SQL parameters are recorded payloads.
            "hide_parameters": True,
        }
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def iter_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
