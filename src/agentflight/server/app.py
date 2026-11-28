"""FastAPI application: span ingest and run queries."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator, Mapping
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from agentflight import __version__
from agentflight.redaction import Policy, PolicyError
from agentflight.server import otlp, service
from agentflight.server.auth import RedactingFilter, authorize, require
from agentflight.server.db import make_engine, session_factory
from agentflight.server.limits import BodySizeLimit, PayloadTooLarge, check_spans
from agentflight.server.schemas import (
    EventOut,
    IngestRequest,
    IngestResponse,
    RunDetail,
    RunList,
)
from agentflight.server.settings import Permission, Settings
from agentflight.server.viewer import install_viewer


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


def _join_errors(errors: list[str], limit: int = 10) -> str:
    more = len(errors) - limit
    return "; ".join(errors[:limit]) + (f"; and {more} more" if more > 0 else "")


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

    policy: Policy | None = None
    if settings.redaction_file:
        try:
            policy = Policy.from_file(settings.redaction_file, on_error=settings.redaction_on_error)
        except PolicyError as exc:
            raise RuntimeError(f"AGENTFLIGHT_REDACTION_FILE: {exc}") from exc
        logging.getLogger("agentflight").info(
            "server-side redaction on: %d rule(s) from %s",
            len(policy.rules),
            settings.redaction_file,
        )

    app = FastAPI(title="AgentFlight", version=__version__)
    app.state.settings = settings
    app.state.redaction = policy

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 body echoes the offending input, which can be a recorded
        # payload; report only where and what.
        detail = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()[:20]
        ]
        return JSONResponse({"detail": detail}, status_code=422)

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
        redacted = service.redact_spans(policy, body.spans, settings.max_attribute_chars)
        run_ids = service.ingest_spans(session, redacted.spans) if redacted.spans else []
        return IngestResponse(
            accepted=len(redacted.spans),
            runs=run_ids,
            rejected=len(redacted.rejected),
            error_message=_join_errors(redacted.rejected),
        )

    def store_otlp(body: bytes, fmt: str, encoding: str | None) -> otlp.Converted:
        raw = otlp.decompress(body, encoding, settings.max_body_bytes)
        request = otlp.parse(raw, fmt)
        count = otlp.span_count(request)
        if count > settings.max_spans_per_batch:
            raise otlp.OTLPError(
                413,
                otlp.RESOURCE_EXHAUSTED,
                f"{count} spans in one request; the limit is {settings.max_spans_per_batch}",
            )
        converted = otlp.convert(request, settings)
        if policy is not None and converted.spans:
            redacted = service.redact_spans(policy, converted.spans, settings.max_attribute_chars)
            for why in redacted.rejected:
                converted.reject(why)
            converted.spans = redacted.spans
        if converted.spans:
            with app.state.sessions() as session:
                service.ingest_spans(session, converted.spans, converted.roots)
        return converted

    @app.post(
        "/v1/traces",
        summary="OTLP/HTTP trace ingest (protobuf or JSON, optionally gzip)",
        response_class=Response,
    )
    async def otlp_traces(request: Request) -> Response:
        fmt = otlp.request_format(request.headers.get("content-type"))
        reply = fmt or otlp.JSON

        def error(
            status: int, code: int, message: str, headers: Mapping[str, str] | None = None
        ) -> Response:
            body = otlp.encode_status(reply, code, message)
            return Response(body, status_code=status, media_type=reply, headers=headers)

        denied = authorize(request, Permission.INGEST)
        if denied is not None:
            code = otlp.UNAUTHENTICATED if denied.status_code == 401 else otlp.PERMISSION_DENIED
            return error(denied.status_code, code, str(denied.detail), denied.headers)
        if fmt is None:
            return error(
                415,
                otlp.INVALID_ARGUMENT,
                f"Content-Type must be {otlp.PROTOBUF} or {otlp.JSON}",
            )
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
        except PayloadTooLarge as exc:
            return error(413, otlp.RESOURCE_EXHAUSTED, str(exc.detail))
        encoding = request.headers.get("content-encoding")
        try:
            converted = await run_in_threadpool(store_otlp, bytes(body), fmt, encoding)
        except otlp.OTLPError as exc:
            return error(exc.status, exc.code, exc.message)
        except (OperationalError, InterfaceError, IntegrityError):
            # Transient (database down, or a concurrent insert of the same run): retryable.
            logging.getLogger("agentflight").warning("OTLP ingest failed", exc_info=True)
            return error(
                503, otlp.UNAVAILABLE, "database unavailable, retry later", {"Retry-After": "5"}
            )
        return Response(otlp.encode_response(fmt, converted), media_type=fmt)

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
        parent_run_id: Annotated[
            str | None,
            Query(pattern=r"^[0-9a-f]{32}$", description="only sub-agent runs started by this run"),
        ] = None,
        has_parent: Annotated[
            bool | None,
            Query(description="true: only sub-agent runs; false: only top-level runs"),
        ] = None,
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
            parent_run_id=parent_run_id,
            has_parent=has_parent,
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

    install_viewer(app, settings)
    return app
