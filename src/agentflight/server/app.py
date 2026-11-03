"""FastAPI application: span ingest and run queries."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from agentflight import __version__
from agentflight.server import service
from agentflight.server.db import make_engine, session_factory
from agentflight.server.schemas import IngestRequest, IngestResponse, RunDetail, RunList


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the app. Pass an engine to override ``AGENTFLIGHT_DATABASE_URL`` (tests)."""
    app = FastAPI(title="AgentFlight", version=__version__)
    app.state.engine = engine or make_engine()
    app.state.sessions = session_factory(app.state.engine)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/spans", response_model=IngestResponse, status_code=202)
    def ingest(body: IngestRequest, session: SessionDep) -> IngestResponse:
        run_ids = service.ingest_spans(session, body.spans)
        return IngestResponse(accepted=len(body.spans), runs=run_ids)

    @app.get("/v1/runs", response_model=RunList)
    def list_runs(
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: str | None = None,
    ) -> RunList:
        runs, total = service.list_runs(session, limit=limit, offset=offset, status=status)
        return RunList(runs=runs, total=total)

    @app.get("/v1/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str, session: SessionDep) -> RunDetail:
        run = service.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    return app
