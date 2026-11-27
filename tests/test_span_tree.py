"""Span tree order for the viewer: nesting, sibling order, orphans and cycles, paging."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from agentflight.server import create_app, service
from agentflight.server.settings import Settings
from agentflight.server.viewer import routes

T = "ab" * 16


def sid(c: str) -> str:
    return c * 16


def span(name: str, s: str, parent: str | None, start: int, **kw: Any) -> dict[str, Any]:
    return {
        "trace_id": T,
        "span_id": sid(s),
        "parent_span_id": sid(parent) if parent else None,
        "name": name,
        "start_time_ns": start,
        "end_time_ns": start + 1,
        "attributes": kw.pop("attributes", {}),
        **kw,
    }


SPANS = [
    span("run", "0", None, 0, attributes={"agentflight.kind": "run"}),
    span("a", "a", "0", 10),
    span("a1", "1", "a", 12),
    span("a2", "2", "a", 11),  # starts before a1, so it comes first
    span("b", "b", "0", 5),  # starts before a
    span("b-child", "3", "b", 6),
    span("orphan", "4", "9", 3),  # parent never arrived
    span("self", "5", "5", 4),  # its own parent
    span("cycle-x", "6", "7", 20),
    span("cycle-y", "7", "6", 21),
]

EXPECTED = [
    ("run", 0),
    ("b", 1),
    ("b-child", 2),
    ("a", 1),
    ("a2", 2),
    ("a1", 2),
    ("orphan", 0),
    ("self", 0),
    ("cycle-x", 0),
    ("cycle-y", 1),
]


@pytest.fixture
def ui(engine: Engine) -> TestClient:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    assert c.post("/v1/spans", json={"spans": SPANS}).status_code == 202
    return c


def test_span_tree_order_nests_children_under_parents(ui: TestClient, engine: Engine) -> None:
    names = {sid(s["span_id"][0]): s["name"] for s in SPANS}
    with Session(engine) as session:
        order = service.span_tree_order(session, T)
    assert [(names[s], depth) for s, depth in order] == EXPECTED


def test_span_tree_order_of_an_empty_or_unknown_run(engine: Engine) -> None:
    with Session(engine) as session:
        assert service.span_tree_order(session, "cd" * 16) == []
        assert service.get_run_tree_page(session, "cd" * 16, offset=0, limit=10) is None


def _rows(html: str) -> list[tuple[str, int]]:
    rows = re.findall(r'data-depth="(\d+)">.*?hx-target="#detail">([^<]*)</button>', html, re.S)
    return [(name, int(depth)) for depth, name in rows]


def test_run_page_shows_the_indented_tree(ui: TestClient) -> None:
    page = ui.get(f"/ui/runs/{T}")
    assert page.status_code == 200
    assert _rows(page.text) == EXPECTED
    assert 'class="tree d0"' in page.text and 'class="tree d2"' in page.text
    assert page.text.count('class="elbow"') == sum(1 for _, d in EXPECTED if d)
    assert "style=" not in page.text  # indentation is classes only (CSP: style-src 'self')


def test_tree_pages_continue_where_the_last_one_stopped(
    ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "EVENTS_PAGE", 4)
    seen = _rows(ui.get(f"/ui/runs/{T}").text)
    offset = 4
    while offset < len(EXPECTED):
        more = ui.get(f"/ui/runs/{T}/events", params={"offset": offset})
        assert "<html" not in more.text
        seen += _rows(more.text)
        offset += 4
    assert seen == EXPECTED


def test_deep_trees_cap_the_indent_class(engine: Engine) -> None:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    chain = [span("run", "0", None, 0, attributes={"agentflight.kind": "run"})]
    hexdigits = "123456789abcdef"
    for i, ch in enumerate(hexdigits):
        chain.append(span(f"level{i + 1}", ch, "0" if i == 0 else hexdigits[i - 1], i + 1))
    assert c.post("/v1/spans", json={"spans": chain}).status_code == 202
    page = c.get(f"/ui/runs/{T}")
    assert [d for _, d in _rows(page.text)] == list(range(16))
    assert 'class="tree d12"' in page.text and "d13" not in page.text


def test_tree_names_stay_escaped(engine: Engine) -> None:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    xss = "<script>alert(1)</script>"
    spans = [
        span("run", "0", None, 0, attributes={"agentflight.kind": "run"}),
        span(xss, "1", "0", 1),
    ]
    assert c.post("/v1/spans", json={"spans": spans}).status_code == 202
    page = c.get(f"/ui/runs/{T}")
    assert xss not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
