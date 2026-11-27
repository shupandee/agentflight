"""FastAPI application: span ingest and run queries."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from agentflight import __version__
from agentflight.server import service
from agentflight.server.auth import RedactingFilter, require
from agentflight.server.db import make_engine, session_factory
from agentflight.server.limits import BodySizeLimit, check_spans
from agentflight.server.schemas import (
    EventOut,
    IngestRequest,
    IngestResponse,
    RunDetail,
    RunList,
)
from agentflight.server.settings import Permission, Settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

RunStatus = Literal["running", "ok", "error", "unknown", "abandoned"]
NAME_HELP = "case-insensitive substring of the run name"
META_HELP = "metadata filter `key:value` (string values, exact match); repeat for AND"
_META_KEY = re.compile(service.META_KEY_PATTERN)


def parse_meta(raw: list[str]) -> tuple[tuple[str, str], ...]:
    out = []
    for item in raw:
        key, sep, value = item.partition(":")
        if not sep or not _META_KEY.match(key):
            raise HTTPException(
                status_code=422,
                detail="meta must be key:value with a key of letters, digits, _ . or -",
            )
        out.append((key, value))
    return tuple(out)


CanIngest = Depends(require(Permission.INGEST))
CanRead = Depends(require(Permission.READ))
CanDelete = Depends(require(Permission.DELETE))

_LOG_FILTER = RedactingFilter(())
LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "agentflight")


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app.

    ``engine`` overrides ``AGENTFLIGHT_DATABASE_URL`` and ``settings`` overrides the
    ``AGENTFLIGHT_*`` environment (tests). Refuses to start without an API key unless
    authentication is explicitly disabled.
    """
    settings = settings or Settings.from_env()
    settings.validate()
    if settings.auth_disabled:
        logging.getLogger("agentflight").warning(
            "AgentFlight auth is DISABLED (AGENTFLIGHT_AUTH_DISABLED); local development only"
        )
    _LOG_FILTER.secrets.extend(k for k in settings.api_keys if k not in _LOG_FILTER.secrets)
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if _LOG_FILTER not in logger.filters:
            logger.addFilter(_LOG_FILTER)

    app = FastAPI(title="AgentFlight", version=__version__)
    app.state.settings = settings
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.state.engine = engine or make_engine()
    app.state.sessions = session_factory(app.state.engine)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    def ready(session: SessionDep) -> dict[str, str]:
        """Readiness: the database is reachable and migrated."""
        try:
            session.execute(text("SELECT 1 FROM runs LIMIT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database not ready") from exc
        return {"status": "ready"}

    @app.post("/v1/spans", response_model=IngestResponse, status_code=202, dependencies=[CanIngest])
    def ingest(body: IngestRequest, session: SessionDep) -> IngestResponse:
        check_spans(body.spans, settings)
        run_ids = service.ingest_spans(session, body.spans)
        return IngestResponse(accepted=len(body.spans), runs=run_ids)

    @app.get("/v1/runs", response_model=RunList, dependencies=[CanRead])
    def list_runs(
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[RunStatus | None, Query()] = None,
        name: Annotated[str | None, Query(max_length=255, description=NAME_HELP)] = None,
        since_ns: Annotated[int | None, Query(description="started at or after (Unix ns)")] = None,
        until_ns: Annotated[int | None, Query(description="started before (Unix ns)")] = None,
        has_errors: bool | None = None,
        model: Annotated[str | None, Query(max_length=255)] = None,
        tool: Annotated[str | None, Query(max_length=255)] = None,
        meta: Annotated[list[str] | None, Query(max_length=5, description=META_HELP)] = None,
    ) -> RunList:
        filters = service.RunFilters(
            status=status,
            name=name,
            since_ns=since_ns,
            until_ns=until_ns,
            has_errors=has_errors,
            model=model,
            tool=tool,
            metadata=parse_meta(meta or []),
        )
        runs, total = service.list_runs(
            session,
            limit=limit,
            offset=offset,
            filters=filters,
            abandoned_before_ns=settings.abandoned_before_ns(time.time_ns()),
        )
        return RunList(runs=runs, total=total)

    @app.get("/v1/runs/{run_id}", response_model=RunDetail, dependencies=[CanRead])
    def get_run(
        run_id: str,
        session: SessionDep,
        events_limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
        events_offset: Annotated[int, Query(ge=0)] = 0,
        payloads: bool = True,
    ) -> RunDetail:
        run = service.get_run(
            session,
            run_id,
            events_limit=events_limit,
            events_offset=events_offset,
            payloads=payloads,
            abandoned_before_ns=settings.abandoned_before_ns(time.time_ns()),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/v1/runs/{run_id}/events/{span_id}", response_model=EventOut, dependencies=[CanRead])
    def get_event(run_id: str, span_id: str, session: SessionDep) -> EventOut:
        event = service.get_event(session, run_id, span_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        return event

    @app.delete("/v1/runs/{run_id}", status_code=204, dependencies=[CanDelete])
    def delete_run(run_id: str, session: SessionDep) -> Response:
        if not service.delete_run(session, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return Response(status_code=204)

    return app
