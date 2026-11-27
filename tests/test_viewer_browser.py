"""Browser smoke test of the viewer against a real server (skipped without Playwright).

    pip install playwright && playwright install chromium
    AGENTFLIGHT_SCREENSHOT_DIR=shots pytest tests/test_viewer_browser.py

Checks that sign-in, the run list, htmx filtering, the run timeline and the event panel
work in a real browser under the viewer's Content-Security-Policy, with no CSP violations.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
FULL_KEY = "browser-full-key-0123456789abcdef"
READ_KEY = "browser-read-key-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = tmp_path_factory.mktemp("browser") / "viewer.db"
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "AGENTFLIGHT_DATABASE_URL": f"sqlite:///{db}",
        "AGENTFLIGHT_API_KEY": FULL_KEY,
        "AGENTFLIGHT_API_KEYS": f"{READ_KEY}:read",
        "AGENTFLIGHT_SESSION_SECRET": "x" * 40,
        "AGENTFLIGHT_ENDPOINT": endpoint,
    }
    env.pop("AGENTFLIGHT_AUTH_DISABLED", None)
    alembic = [sys.executable, "-m", "alembic", "upgrade", "head"]
    subprocess.run(alembic, cwd=ROOT, env=env, check=True, capture_output=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentflight.server.main:app", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{endpoint}/ready", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("server did not become ready")
        for example in ("toy_agent.py", "replay_agent.py"):
            subprocess.run(
                [sys.executable, str(ROOT / "examples" / example)],
                cwd=ROOT / "examples",
                env=env,
                check=True,
                capture_output=True,
            )
        yield endpoint
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium not available: {exc}")
        yield b
        b.close()


def test_viewer_in_a_real_browser(server: str, browser: object) -> None:
    shots = os.environ.get("AGENTFLIGHT_SCREENSHOT_DIR")
    context = browser.new_context(viewport={"width": 1440, "height": 900})  # type: ignore[attr-defined]
    page = context.new_page()
    problems: list[str] = []
    page.on(
        "console",
        lambda m: (
            problems.append(m.text)
            if m.type == "error" or "Content Security Policy" in m.text
            else None
        ),
    )
    page.on("pageerror", lambda e: problems.append(str(e)))

    page.goto(f"{server}/ui")
    assert page.url.endswith("/ui/login")
    page.fill("#key", READ_KEY)
    page.click("button[type=submit]")
    page.wait_for_url(f"{server}/ui")
    cookies = {c["name"]: c for c in context.cookies()}
    session = cookies["agentflight_session"]
    assert session["httpOnly"] and session["sameSite"] == "Strict"
    assert READ_KEY not in session["value"]

    auth = {"Authorization": f"Bearer {READ_KEY}"}
    total = httpx.get(f"{server}/v1/runs", headers=auth).json()["total"]
    with_errors = httpx.get(
        f"{server}/v1/runs", params={"has_errors": "true"}, headers=auth
    ).json()["total"]
    assert 0 < with_errors < total
    rows = page.locator("table.runs tbody tr")
    assert rows.count() == total
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{shots}/run-list.png", full_page=True)

    # htmx swaps the table in place when a filter changes.
    page.select_option("select[name=has_errors]", "yes")
    # (Playwright's wait_for_function needs eval, which the CSP rightly blocks.)
    sync_api.expect(rows).to_have_count(with_errors)
    assert page.url.endswith("/ui")  # no navigation, and no history entry
    page.select_option("select[name=has_errors]", "")
    sync_api.expect(rows).to_have_count(total)

    page.locator("table.runs tbody tr.has-errors a").first.click()
    page.wait_for_selector("table.events")
    assert page.locator("tr.err").count() >= 1
    page.locator("tr.k-tool.err button.link").first.click()
    page.wait_for_selector("#detail article.event")
    assert "Traceback" in page.inner_text("#detail")
    page.locator("tr.k-llm button.link").first.click()
    page.wait_for_selector("#detail h3:has-text('fake-model-1')")
    assert "agentflight.llm.messages" in page.inner_text("#detail")
    if shots:
        page.screenshot(path=f"{shots}/run-page.png", full_page=True)

    fork_id = next(
        r["id"]
        for r in httpx.get(f"{server}/v1/runs", headers=auth).json()["runs"]
        if httpx.get(f"{server}/v1/runs/{r['id']}/events/{r['root_span_id']}", headers=auth)
        .json()["attributes"]
        .get("agentflight.replay.mode")
        == "fork"
    )
    page.goto(f"{server}/ui/runs/{fork_id}")
    assert "fork of" in page.inner_text("p.replay")
    if shots:
        page.screenshot(path=f"{shots}/fork-run.png", full_page=True)

    page.click("text=Sign out")
    page.wait_for_url(f"{server}/ui/login")
    assert problems == []
    context.close()
