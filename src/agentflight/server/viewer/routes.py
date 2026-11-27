"""Viewer routes: sign-in, sign-out and pages."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from agentflight import __version__
from agentflight.server.auth import permissions_for
from agentflight.server.settings import Permission, Settings
from agentflight.server.viewer.session import (
    COOKIE_NAME,
    COOKIE_PATH,
    SessionSigner,
    current_session,
    is_https,
)

HERE = Path(__file__).parent
LOGIN_PATH = "/ui/login"

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
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        # Browsers send Origin on cross-site POSTs; a missing one is a same-origin form or
        # a non-browser client, which can't ride on a victim's cookie anyway.
        return origin is None
    return urlparse(origin).netloc == request.headers.get("host")


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


@router.get("", response_class=HTMLResponse)
def index(request: Request) -> Response:
    require_viewer(request)
    return _render(request, "index.html")


def install_viewer(app: FastAPI, settings: Settings) -> None:
    secret = settings.session_secret.encode() if settings.session_secret else None
    app.state.session_signer = SessionSigner(
        secret or secrets.token_bytes(32), max_age_seconds=int(settings.session_hours * 3600)
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
