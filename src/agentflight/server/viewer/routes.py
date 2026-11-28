"""Viewer routes: sign-in, sign-out and pages."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from agentflight import __version__
from agentflight.server import service
from agentflight.server.auth import permissions_for
from agentflight.server.models import Run
from agentflight.server.schemas import RunSummary
from agentflight.server.settings import Permission, Settings
from agentflight.server.viewer.format import FILTERS, parse_filters, replay_info, timeline_scale
from agentflight.server.viewer.session import (
    COOKIE_NAME,
    COOKIE_PATH,
    SessionSigner,
    current_session,
    derive_session_secret,
    is_https,
)

HERE = Path(__file__).parent
LOGIN_PATH = "/ui/login"
RUNS_PAGE = 50
EVENTS_PAGE = 100
CHILD_RUNS_SHOWN = 50

# No inline script or style anywhere; everything comes from /ui/static.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer: with no-referrer browsers send "Origin: null" on form
    # posts, which the sign-in origin check has to refuse.
    "Referrer-Policy": "same-origin",
}


def make_environment() -> Environment:
    # Autoescape every template, whatever its extension. Templates never use |safe.
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["version"] = __version__
    env.filters.update(FILTERS)
    return env


class LoginRequired(Exception):
    pass


def _render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    env: Environment = request.app.state.viewer_env
    settings: Settings = request.app.state.settings
    html = env.get_template(name).render(
        auth_disabled=settings.auth_disabled, path=request.url.path, **context
    )
    return HTMLResponse(html, status_code=status_code)


def require_viewer(request: Request) -> None:
    """Pages need a valid read-only session, unless auth is disabled."""
    if request.app.state.settings.auth_disabled:
        return
    if current_session(request) is None:
        raise LoginRequired


def _same_origin(request: Request) -> bool:
    """CSRF check for the sign-in and sign-out posts. Fails closed.

    Current browsers send ``Origin`` on every form POST, same-origin ones included, so a
    request without it (or with ``Origin: null``, e.g. from a sandboxed frame or a
    ``no-referrer`` page) is refused rather than trusted. A proxy in front must pass the
    original ``Host`` through.
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or origin == "null" or not host:
        return False
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == host


router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if request.app.state.settings.auth_disabled or current_session(request) is not None:
        return RedirectResponse("/ui", status_code=303)
    return _render(request, "login.html", error=None)


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if settings.auth_disabled:
        return RedirectResponse("/ui", status_code=303)
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin sign-in refused")
    form = parse_qs((await request.body()).decode("utf-8", "replace"), max_num_fields=5)
    key = (form.get("key") or [""])[0].strip()
    perms = permissions_for(key, settings.api_keys) if key else frozenset()
    if Permission.READ not in perms:
        error = "That key is not valid." if not perms else "That key has no read permission."
        return _render(request, "login.html", status_code=401, error=error)
    signer: SessionSigner = request.app.state.session_signer
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        signer.issue(key),
        max_age=signer.max_age_seconds,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=is_https(request, settings),
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin sign-out refused")
    settings: Settings = request.app.state.settings
    response = RedirectResponse(LOGIN_PATH if not settings.auth_disabled else "/ui", 303)
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=is_https(request, settings),
    )
    return response


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


DB = Annotated[Session, Depends(_session)]
Viewer = Depends(require_viewer)


def _abandoned_before(request: Request) -> int | None:
    settings: Settings = request.app.state.settings
    return settings.abandoned_before_ns(time.time_ns())


def _run_list(request: Request, db: Session) -> dict[str, Any]:
    form = parse_filters(request.query_params)
    runs: list[RunSummary] = []
    total = 0
    if not form.errors:
        runs, total = service.list_runs(
            db,
            limit=RUNS_PAGE,
            offset=form.offset,
            filters=form.filters,
            abandoned_before_ns=_abandoned_before(request),
        )
    return {
        "form": form,
        "runs": [r.model_dump() for r in runs],
        "total": total,
        "page_size": RUNS_PAGE,
        "statuses": service.STATUSES,
    }


@router.get("", response_class=HTMLResponse, dependencies=[Viewer])
def run_list_page(request: Request, db: DB) -> Response:
    return _render(request, "runs.html", **_run_list(request, db))


@router.get("/runs", response_class=HTMLResponse, dependencies=[Viewer])
def run_list_table(request: Request, db: DB) -> Response:
    """The run table alone, for htmx swaps; a plain visit gets the full page."""
    if not request.headers.get("hx-request"):
        query = request.url.query
        return RedirectResponse("/ui" + (f"?{query}" if query else ""), status_code=303)
    return _render(request, "_runs_table.html", **_run_list(request, db))


def _timeline_page(
    request: Request, db: Session, run_id: str, offset: int
) -> dict[str, Any] | None:
    # Paged and payload-free, like the run API, but in span tree order with depths.
    found = service.get_run_tree_page(
        db, run_id, offset=offset, limit=EVENTS_PAGE, abandoned_before_ns=_abandoned_before(request)
    )
    if found is None:
        return None
    run, depths = found
    children = service.child_runs(
        db, run_id, limit=CHILD_RUNS_SHOWN, abandoned_before_ns=_abandoned_before(request)
    )
    spawned_at: dict[str, list[dict[str, Any]]] = {}
    for child in children:
        if child.parent_span_id:
            spawned_at.setdefault(child.parent_span_id, []).append(child.model_dump())
    parent = db.get(Run, run.parent_run_id) if run.parent_run_id else None
    detail = run.model_dump()
    for event, depth in zip(detail["events"], depths, strict=True):
        event["depth"] = depth
    return {
        "run": detail,
        "events": detail["events"],
        "scale": timeline_scale(detail),
        "children": [c.model_dump() for c in children],
        "spawned_at": spawned_at,
        "parent_name": parent.name if parent else None,
        "parent_known": parent is not None,
        "next_offset": offset + EVENTS_PAGE if offset + EVENTS_PAGE < run.event_count else None,
    }


@router.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Viewer])
def run_page(request: Request, db: DB, run_id: str) -> Response:
    page = _timeline_page(request, db, run_id, 0)
    if page is None:
        return _render(request, "not_found.html", status_code=404, what="run")
    root = None
    root_span_id = page["run"].get("root_span_id")
    if root_span_id:
        event = service.get_event(db, run_id, root_span_id)
        root = event.model_dump() if event else None
    return _render(request, "run.html", replay=replay_info(root), **page)


@router.get("/runs/{run_id}/events", response_class=HTMLResponse, dependencies=[Viewer])
def run_events(
    request: Request, db: DB, run_id: str, offset: Annotated[int, Query(ge=0)] = 0
) -> Response:
    """More timeline rows (htmx "load more")."""
    page = _timeline_page(request, db, run_id, offset)
    if page is None:
        return _render(request, "not_found.html", status_code=404, what="run")
    return _render(request, "_timeline_rows.html", **page)


@router.get("/runs/{run_id}/events/{span_id}", response_class=HTMLResponse, dependencies=[Viewer])
def event_panel(request: Request, db: DB, run_id: str, span_id: str) -> Response:
    """One event with all of its payloads, loaded when a timeline row is opened."""
    event = service.get_event(db, run_id, span_id)
    if event is None:
        return _render(request, "not_found.html", status_code=404, what="event")
    template = "_event.html" if request.headers.get("hx-request") else "event.html"
    return _render(request, template, run_id=run_id, event=event.model_dump())


def install_viewer(app: FastAPI, settings: Settings) -> None:
    if settings.session_secret:
        secret = settings.session_secret.encode()
    elif settings.api_keys:
        secret = derive_session_secret(settings.api_keys)
    else:  # auth disabled: there are no sessions to sign
        secret = secrets.token_bytes(32)
    app.state.session_signer = SessionSigner(
        secret, max_age_seconds=int(settings.session_hours * 3600)
    )
    app.state.viewer_env = make_environment()

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            # htmx swaps would put the login page inside the current one; redirect the page.
            return Response(status_code=401, headers={"HX-Redirect": LOGIN_PATH})
        return RedirectResponse(LOGIN_PATH, status_code=303)

    @app.middleware("http")
    async def viewer_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            response.headers.update(SECURITY_HEADERS)
            if not request.url.path.startswith("/ui/static/"):
                # Pages show recorded payloads; keep them out of shared caches and history.
                response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    app.mount("/ui/static", StaticFiles(directory=HERE / "static"), name="viewer-static")
