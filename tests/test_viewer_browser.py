"""Browser smoke test of the viewer against a real server (skipped without Playwright).

    pip install playwright && playwright install chromium
    AGENTFLIGHT_SCREENSHOT_DIR=shots pytest tests/test_viewer_browser.py

Checks that sign-in, the run list, htmx filtering, the run timeline and the event panel
work in a real browser under the viewer's Content-Security-Policy, with no CSP violations.
"""

from __future__ import annotations

import itertools
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
FULL_KEY = "browser-full-key-0123456789abcdef0123456789"
READ_KEY = "browser-read-key-0123456789abcdef0123456789"


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
        for example in ("toy_agent.py", "replay_agent.py", "langgraph_agent.py"):
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

    # The span tree: LangGraph nests model and tool calls under graph nodes.
    graph_id = next(
        r["id"]
        for r in httpx.get(f"{server}/v1/runs", headers=auth).json()["runs"]
        if r["name"] == "langgraph-weather-agent"
    )
    page.goto(f"{server}/ui/runs/{graph_id}")
    tree_rows = page.locator("table.events tbody tr[data-depth]")
    depths = [int(d) for d in tree_rows.evaluate_all("rs => rs.map(r => r.dataset.depth)")]
    assert depths[0] == 0 and max(depths) >= 2
    assert all(b <= a + 1 for a, b in itertools.pairwise(depths))  # no jumps
    lefts: dict[int, float] = {}
    for i, depth in enumerate(depths):
        box = tree_rows.nth(i).locator("button.link").bounding_box()
        assert box is not None
        lefts.setdefault(depth, box["x"])
    # Deeper rows really are indented on screen (the CSS classes apply under the CSP).
    assert lefts[0] < lefts[1] < lefts[2]
    if shots:
        page.screenshot(path=f"{shots}/span-tree.png", full_page=True)

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


def test_sub_agent_runs_in_a_real_browser(server: str, browser: object) -> None:
    """Parent, child, missing-parent and inferred-parent pages, and the has_parent toggle."""
    import secrets

    import agentflight

    agentflight.configure(server, api_key=FULL_KEY, batch=False)
    try:

        @agentflight.tool
        def delegate(topic: str) -> str:
            with agentflight.run("researcher", metadata={"topic": topic}):
                agentflight.decision("source", "web", reason="needs recent facts")
            return "notes"

        with agentflight.run("planner") as parent:
            delegate("otel")
            with agentflight.run("writer"):
                agentflight.state_change("draft", "v1")
        missing_parent = secrets.token_hex(16)
        with agentflight.run("orphan-worker", parent_run_id=missing_parent) as orphan:
            agentflight.decision("retry", "no")
        agentflight.flush()
    finally:
        agentflight.shutdown()

    # An OpenTelemetry-only job linked (not parented) to the planner: an inferred parent.
    job_trace = secrets.token_hex(16)
    otlp_job = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "queue"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": job_trace,
                                "spanId": secrets.token_hex(8),
                                "name": "nightly-summary",
                                "kind": 5,
                                "startTimeUnixNano": str(time.time_ns()),
                                "endTimeUnixNano": str(time.time_ns() + 5_000_000),
                                "links": [
                                    {"traceId": parent.run_id, "spanId": parent_span(parent)}
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    full = {"Authorization": f"Bearer {FULL_KEY}"}
    assert httpx.post(f"{server}/v1/traces", json=otlp_job, headers=full).status_code == 200

    auth = {"Authorization": f"Bearer {READ_KEY}"}
    kids = httpx.get(f"{server}/v1/runs", params={"parent_run_id": parent.run_id}, headers=auth)
    by_name = {r["name"]: r for r in kids.json()["runs"]}
    assert set(by_name) == {"researcher", "writer", "nightly-summary"}
    assert by_name["nightly-summary"]["parent_source"] == "inferred"
    assert by_name["researcher"]["parent_source"] == "explicit"

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
    page.goto(f"{server}/ui/login")
    page.fill("#key", READ_KEY)
    page.click("button[type=submit]")
    page.wait_for_url(f"{server}/ui")

    # The run list can hide sub-agent runs (htmx swap, no navigation).
    rows = page.locator("table.runs tbody tr")
    total = httpx.get(f"{server}/v1/runs", headers=auth).json()["total"]
    top = httpx.get(f"{server}/v1/runs", params={"has_parent": "false"}, headers=auth)
    top_total = top.json()["total"]
    assert 0 < top_total < total
    sync_api.expect(rows).to_have_count(total)
    page.select_option("select[name=has_parent]", "false")
    sync_api.expect(rows).to_have_count(top_total)
    assert page.url.endswith("/ui")
    assert "sub-agent run of" not in page.inner_text("table.runs")
    page.select_option("select[name=has_parent]", "")
    sync_api.expect(rows).to_have_count(total)

    # Parent: lists its sub-agent runs and marks the span each one started from.
    page.goto(f"{server}/ui/runs/{parent.run_id}")
    children = page.locator("details.children li")
    sync_api.expect(children).to_have_count(3)
    assert "3 sub-agent runs" in page.inner_text("details.children summary")
    assert page.locator("a.spawned").count() == 3
    assert "(started from this run, inferred)" in page.inner_text("details.children")
    if shots:
        page.screenshot(path=f"{shots}/sub-agent-parent.png", full_page=True)

    # Child: links back to the parent and the span it was started from.
    page.locator("details.children a", has_text="researcher").click()
    page.wait_for_selector("p.lineage")
    lineage = page.inner_text("p.lineage")
    assert "sub-agent run of" in lineage and "planner" in lineage and "from span" in lineage
    if shots:
        page.screenshot(path=f"{shots}/sub-agent-child.png", full_page=True)
    page.click("p.lineage a")
    page.wait_for_url(f"{server}/ui/runs/{parent.run_id}")

    # Inferred: worded as "started from run", not "sub-agent run of".
    page.goto(f"{server}/ui/runs/{job_trace}")
    lineage = page.inner_text("p.lineage")
    assert "started from run" in lineage and "sub-agent run of" not in lineage
    if shots:
        page.screenshot(path=f"{shots}/sub-agent-inferred.png", full_page=True)

    # Missing parent: the child still loads and says so; no link to a page that 404s.
    page.goto(f"{server}/ui/runs/{orphan.run_id}")
    lineage = page.inner_text("p.lineage")
    assert "parent not recorded" in lineage and missing_parent in lineage
    assert page.locator("p.lineage a").count() == 0
    assert page.locator("table.events tbody tr[data-depth]").count() == 2
    if shots:
        page.screenshot(path=f"{shots}/sub-agent-missing-parent.png", full_page=True)

    assert problems == []
    context.close()


def parent_span(handle: object) -> str:
    """The run span id of a ``RunHandle``, as OTLP/JSON hex."""
    return format(handle.span.get_span_context().span_id, "016x")  # type: ignore[attr-defined]
