"""Viewer sign-in: signed read-only sessions in a hardened cookie."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from agentflight.server import create_app
from agentflight.server.settings import Permission, Settings, parse_api_keys
from agentflight.server.viewer.routes import CONTENT_SECURITY_POLICY
from agentflight.server.viewer.session import COOKIE_NAME, SessionSigner, derive_session_secret
from tests.conftest import TEST_KEY

READ_KEY = "viewer-read-key-0123456789abcdef0123456789"
SAME_ORIGIN = {"Origin": "http://testserver"}  # what a browser sends from the login page
INGEST_KEY = "viewer-ingest-key-0123456789abcdef0123456789"
SECRET = "s" * 40
TEMPLATES = Path(__file__).resolve().parents[1] / "src/agentflight/server/viewer/templates"


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "api_key": TEST_KEY,
        "scoped_keys": parse_api_keys(f"{READ_KEY}:read,{INGEST_KEY}:ingest"),
        "session_secret": SECRET,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def ui(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, settings()), follow_redirects=False)


def sign_in(c: TestClient, key: str = READ_KEY) -> str:
    resp = c.post("/ui/login", data={"key": key}, headers=SAME_ORIGIN)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/ui"
    return resp.headers["set-cookie"]


def test_pages_need_a_session(ui: TestClient) -> None:
    resp = ui.get("/ui")
    assert (resp.status_code, resp.headers["location"]) == (303, "/ui/login")
    htmx = ui.get("/ui", headers={"HX-Request": "true"})
    assert (htmx.status_code, htmx.headers["hx-redirect"]) == (401, "/ui/login")
    assert ui.get("/ui/login").status_code == 200


def test_read_key_signs_in_with_a_hardened_cookie(ui: TestClient) -> None:
    cookie = sign_in(ui)
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert "httponly" in parts
    assert "samesite=strict" in parts
    assert "path=/ui" in parts
    assert "secure" not in parts  # plain http
    assert any(p.startswith("max-age=") for p in parts)
    assert ui.get("/ui").status_code == 200


def test_cookie_holds_a_signed_session_not_the_key(ui: TestClient) -> None:
    sign_in(ui)
    token = ui.cookies[COOKIE_NAME]
    assert READ_KEY not in token
    body = token.split(".")[0]
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    assert READ_KEY not in decoded
    assert '"perms":["read"]' in decoded


@pytest.mark.parametrize("https", [True, False])
def test_cookie_is_secure_on_https_or_when_forced(engine: Engine, https: bool) -> None:
    app = create_app(engine, settings(cookie_secure=not https))
    base = "https://testserver" if https else "http://testserver"
    c = TestClient(app, base_url=base, follow_redirects=False)
    parts = [p.strip().lower() for p in sign_in(c).split(";")]
    assert "secure" in parts


@pytest.mark.parametrize(
    ("key", "message"),
    [("wrong", "is not valid"), (INGEST_KEY, "has no read permission"), ("", "is not valid")],
)
def test_bad_keys_are_refused(ui: TestClient, key: str, message: str) -> None:
    resp = ui.post("/ui/login", data={"key": key}, headers=SAME_ORIGIN)
    assert resp.status_code == 401
    assert message in resp.text
    assert "set-cookie" not in resp.headers
    assert key not in resp.text or key == ""


def test_full_key_session_is_still_read_only(ui: TestClient) -> None:
    sign_in(ui, TEST_KEY)
    # The cookie is never accepted by the API, so a viewer session can't ingest or delete.
    assert ui.get("/v1/runs").status_code == 401
    assert ui.delete(f"/v1/runs/{'a' * 32}").status_code == 401
    assert ui.post("/v1/spans", json={"spans": []}).status_code == 401


def test_tampered_or_expired_sessions_are_refused(ui: TestClient) -> None:
    sign_in(ui)
    token = ui.cookies[COOKIE_NAME]
    body, sig = token.split(".")
    for bad in [f"{body}.{sig[:-2]}AA", f"{body[:-2]}AA.{sig}", "garbage", ""]:
        ui.cookies.clear()
        ui.cookies.set(COOKIE_NAME, bad, path="/ui")
        assert ui.get("/ui").status_code == 303
    signer = SessionSigner(SECRET.encode(), max_age_seconds=60)
    ui.cookies.clear()
    ui.cookies.set(COOKIE_NAME, signer.issue(READ_KEY, now=0), path="/ui")
    assert ui.get("/ui").status_code == 303
    ui.cookies.clear()
    ui.cookies.set(COOKIE_NAME, signer.issue(READ_KEY), path="/ui")
    assert ui.get("/ui").status_code == 200


def test_removing_the_key_ends_its_sessions(engine: Engine) -> None:
    signer = SessionSigner(SECRET.encode(), max_age_seconds=3600)
    token = signer.issue(READ_KEY)
    still = TestClient(create_app(engine, settings()), follow_redirects=False)
    still.cookies.set(COOKIE_NAME, token, path="/ui")
    assert still.get("/ui").status_code == 200
    gone = TestClient(create_app(engine, settings(scoped_keys=())), follow_redirects=False)
    gone.cookies.set(COOKIE_NAME, token, path="/ui")
    assert gone.get("/ui").status_code == 303


def test_sessions_need_the_same_secret(engine: Engine) -> None:
    token = SessionSigner(b"x" * 40, max_age_seconds=3600).issue(READ_KEY)
    c = TestClient(create_app(engine, settings()), follow_redirects=False)
    c.cookies.set(COOKIE_NAME, token, path="/ui")
    assert c.get("/ui").status_code == 303


def test_sign_out_clears_the_cookie(ui: TestClient) -> None:
    sign_in(ui)
    resp = ui.post("/ui/logout", headers=SAME_ORIGIN)
    assert (resp.status_code, resp.headers["location"]) == (303, "/ui/login")
    assert f'{COOKIE_NAME}=""' in resp.headers["set-cookie"]
    assert ui.get("/ui").status_code == 303


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no Origin at all: fail closed
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": "http://testserver.evil.test"},
        {"Origin": "file://testserver"},
        {"Referer": "http://testserver/ui/login"},  # Referer alone isn't enough
    ],
)
def test_sign_in_without_a_matching_origin_is_refused(
    ui: TestClient, headers: dict[str, str]
) -> None:
    resp = ui.post("/ui/login", data={"key": READ_KEY}, headers=headers)
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers


def test_https_origin_behind_a_tls_proxy_is_accepted(ui: TestClient) -> None:
    # TLS ends at the proxy, so the app sees http while the browser's Origin says https.
    resp = ui.post("/ui/login", data={"key": READ_KEY}, headers={"Origin": "https://testserver"})
    assert resp.status_code == 303


def test_sign_out_without_origin_is_refused(ui: TestClient) -> None:
    sign_in(ui)
    assert ui.post("/ui/logout").status_code == 403
    assert ui.get("/ui").status_code == 200


def test_cross_origin_sign_in_is_refused(ui: TestClient) -> None:
    resp = ui.post("/ui/login", data={"key": READ_KEY}, headers={"Origin": "https://evil.test"})
    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers
    ok = ui.post("/ui/login", data={"key": READ_KEY}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 303


def test_auth_disabled_skips_sign_in(engine: Engine) -> None:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    assert c.get("/ui").status_code == 200
    assert c.get("/ui/login").headers["location"] == "/ui"
    assert c.post("/ui/login", data={"key": "x"}).headers["location"] == "/ui"
    assert "Sign out" not in c.get("/ui").text


def test_security_headers_on_viewer_responses(ui: TestClient) -> None:
    for path in ["/ui/login", "/ui", "/ui/static/viewer.css"]:
        resp = ui.get(path)
        assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
    assert ui.get("/ui/login").headers["cache-control"] == "no-store"
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "content-security-policy" not in ui.get("/health").headers


def test_session_secret_validation() -> None:
    with pytest.raises(RuntimeError, match="32 characters"):
        settings(session_secret="short").validate()


def test_templates_have_no_inline_script_style_or_safe_filter() -> None:
    templates = list(TEMPLATES.glob("*.html"))
    assert templates
    for path in templates:
        text = path.read_text()
        assert "|safe" not in text.replace(" ", ""), path.name
        assert "autoescape false" not in text, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name
        assert not re.search(r"<style", text), path.name
        assert not re.search(r"\sstyle=", text), path.name
        assert not re.search(r"\son[a-z]+=", text), path.name
        assert "hx-on" not in text, path.name


def test_default_secret_is_shared_by_workers_with_the_same_keys(engine: Engine) -> None:
    first = TestClient(create_app(engine, settings(session_secret=None)), follow_redirects=False)
    sign_in(first)
    token = first.cookies[COOKIE_NAME]
    # A second worker (another app instance) with the same keys accepts the session.
    second = TestClient(create_app(engine, settings(session_secret=None)), follow_redirects=False)
    second.cookies.set(COOKIE_NAME, token, path="/ui")
    assert second.get("/ui").status_code == 200


def test_changing_any_key_ends_sessions_with_the_default_secret(engine: Engine) -> None:
    first = TestClient(create_app(engine, settings(session_secret=None)), follow_redirects=False)
    sign_in(first)
    token = first.cookies[COOKIE_NAME]
    rotated = settings(session_secret=None, api_key="r" * 40)  # read key unchanged
    other = TestClient(create_app(engine, rotated), follow_redirects=False)
    other.cookies.set(COOKIE_NAME, token, path="/ui")
    assert other.get("/ui").status_code == 303


def test_session_secret_override_survives_key_changes(engine: Engine) -> None:
    first = TestClient(create_app(engine, settings()), follow_redirects=False)
    sign_in(first)
    token = first.cookies[COOKIE_NAME]
    rotated = TestClient(create_app(engine, settings(api_key="r" * 40)), follow_redirects=False)
    rotated.cookies.set(COOKIE_NAME, token, path="/ui")
    assert rotated.get("/ui").status_code == 200


def test_derived_secret_depends_only_on_the_key_set() -> None:
    a = {"k1": frozenset({Permission.READ}), "k2": frozenset(Permission)}
    b = dict(reversed(list(a.items())))
    assert derive_session_secret(a) == derive_session_secret(b)
    assert derive_session_secret(a) != derive_session_secret({"k1": frozenset(Permission)})
    assert b"k1" not in derive_session_secret(a)
