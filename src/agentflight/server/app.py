"""FastAPI application: span ingest and run queries."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from agentflight import __version__
from agentflight.server import service
from agentflight.server.auth import RedactingFilter, require
from agentflight.server.db import make_engine, session_factory
from agentflight.server.schemas import IngestRequest, IngestResponse, RunDetail, RunList
from agentflight.server.settings import Permission, Settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


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
        run_ids = service.ingest_spans(session, body.spans)
        return IngestResponse(accepted=len(body.spans), runs=run_ids)

    @app.get("/v1/runs", response_model=RunList, dependencies=[CanRead])
    def list_runs(
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: str | None = None,
    ) -> RunList:
        runs, total = service.list_runs(session, limit=limit, offset=offset, status=status)
        return RunList(runs=runs, total=total)

    @app.get("/v1/runs/{run_id}", response_model=RunDetail, dependencies=[CanRead])
    def get_run(run_id: str, session: SessionDep) -> RunDetail:
        run = service.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.delete("/v1/runs/{run_id}", status_code=204, dependencies=[CanDelete])
    def delete_run(run_id: str, session: SessionDep) -> Response:
        if not service.delete_run(session, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return Response(status_code=204)

    return app
