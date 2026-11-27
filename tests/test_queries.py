"""Run list filters, paged run detail and single events."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

A, B, C = "a" * 32, "b" * 32, "c" * 32


def span(trace: str, sid: str, kind: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trace_id": trace,
        "span_id": sid * 16,
        "parent_span_id": None if kind == "run" else "0" * 16,
        "name": f"{kind}-{sid}",
        "start_time_ns": 10,
        "end_time_ns": 20,
        "status": "ok",
        "attributes": {"agentflight.kind": kind, **kw.pop("attributes", {})},
    }
    base.update(kw)
    return base


def run_span(trace: str, name: str, start: int, meta: dict[str, Any], **kw: Any) -> dict[str, Any]:
    attrs = {"agentflight.run.name": name, "agentflight.run.metadata": json.dumps(meta)}
    return span(
        trace, "0", "run", start_time_ns=start, end_time_ns=start + 100, attributes=attrs, **kw
    )


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    spans = [
        run_span(A, "Support_Bot", 1_000, {"env": "prod", "team": "x"}),
        span(A, "1", "llm", start_time_ns=1_010, attributes={"agentflight.llm.model": "gpt-a"}),
        span(A, "2", "tool", start_time_ns=1_020, attributes={"agentflight.tool.name": "search"}),
        run_span(B, "support 100%", 2_000, {"env": "dev", "n": 3}, status="error"),
        span(
            B,
            "1",
            "tool",
            start_time_ns=2_010,
            status="error",
            attributes={"agentflight.tool.name": "fetch"},
        ),
        run_span(C, "research", 3_000, {"env": "prod"}),
        span(C, "1", "llm", start_time_ns=3_010, attributes={"agentflight.llm.model": "gpt-b"}),
    ]
    assert client.post("/v1/spans", json={"spans": spans}).status_code == 202
    return client


def ids(client: TestClient, **params: Any) -> list[str]:
    body = client.get("/v1/runs", params=params).json()
    assert body["total"] == len(body["runs"])
    return [r["id"] for r in body["runs"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, [C, B, A]),
        ({"status": "error"}, [B]),
        ({"status": "ok"}, [C, A]),
        ({"name": "SUPPORT"}, [B, A]),
        ({"name": "%"}, [B]),  # LIKE wildcards are matched literally
        ({"name": "_"}, [A]),
        ({"since_ns": 2_000}, [C, B]),
        ({"until_ns": 2_000}, [A]),
        ({"since_ns": 1_500, "until_ns": 2_500}, [B]),
        ({"has_errors": "true"}, [B]),
        ({"has_errors": "false"}, [C, A]),
        ({"model": "gpt-a"}, [A]),
        ({"model": "gpt"}, []),  # exact match
        ({"tool": "fetch"}, [B]),
        ({"meta": "env:prod"}, [C, A]),
        ({"meta": ["env:prod", "team:x"]}, [A]),
        ({"meta": "env:staging"}, []),
        ({"meta": "missing:prod"}, []),
        ({"model": "gpt-b", "meta": "env:prod", "has_errors": "false"}, [C]),
    ],
)
def test_run_filters(seeded: TestClient, params: dict[str, Any], expected: list[str]) -> None:
    assert ids(seeded, **params) == expected


def test_filtered_total_counts_all_pages(seeded: TestClient) -> None:
    body = seeded.get("/v1/runs", params={"meta": "env:prod", "limit": 1}).json()
    assert (body["total"], [r["id"] for r in body["runs"]]) == (2, [C])


@pytest.mark.parametrize(
    "params",
    [
        {"status": "bogus"},
        {"meta": "nocolon"},
        {"meta": "bad key:x"},
        {"meta": ":x"},
        {"meta": [f"k{i}:v" for i in range(6)]},
        {"name": "x" * 256},
    ],
)
def test_bad_filters_are_422(seeded: TestClient, params: dict[str, Any]) -> None:
    assert seeded.get("/v1/runs", params=params).status_code == 422


def _many_events(client: TestClient, n: int) -> None:
    spans = [run_span(A, "big", 0, {})]
    for i in range(1, n + 1):
        spans.append(
            span(
                A,
                "0",
                "tool",
                span_id=f"{i:016x}",
                start_time_ns=i,
                end_time_ns=i + 1,
                attributes={
                    "agentflight.tool.name": "t",
                    "agentflight.tool.args": json.dumps({"i": i}),
                    "agentflight.tool.result": json.dumps("r" * 50),
                },
                events=[{"name": "exception", "time_ns": i, "attributes": {"a": "b"}}],
            )
        )
    client.post("/v1/spans", json={"spans": spans})


def test_run_detail_is_full_by_default(client: TestClient) -> None:
    _many_events(client, 30)
    detail = client.get(f"/v1/runs/{A}").json()
    assert (len(detail["events"]), detail["event_count"]) == (31, 31)
    assert (detail["events_offset"], detail["events_limit"]) == (0, None)
    assert detail["events"][1]["attributes"]["agentflight.tool.args"] == {"i": 1}


def test_run_detail_pages_events_in_start_order(client: TestClient) -> None:
    _many_events(client, 30)
    seen: list[str] = []
    for offset in range(0, 31, 7):
        page = client.get(
            f"/v1/runs/{A}", params={"events_limit": 7, "events_offset": offset}
        ).json()
        assert (page["events_offset"], page["events_limit"], page["event_count"]) == (
            offset,
            7,
            31,
        )
        seen += [e["span_id"] for e in page["events"]]
    full = [e["span_id"] for e in client.get(f"/v1/runs/{A}").json()["events"]]
    assert seen == full


def test_payloads_off_leaves_payloads_out_and_lists_them(client: TestClient) -> None:
    _many_events(client, 2)
    event = client.get(f"/v1/runs/{A}", params={"payloads": "false"}).json()["events"][1]
    assert event["attributes"] == {"agentflight.kind": "tool", "agentflight.tool.name": "t"}
    assert event["span_events"] == []
    assert sorted(event["omitted"]) == [
        "agentflight.tool.args",
        "agentflight.tool.result",
        "span_events",
    ]
    full = client.get(f"/v1/runs/{A}/events/{event['span_id']}").json()
    assert full["attributes"]["agentflight.tool.args"] == {"i": 1}
    assert full["omitted"] == []
    assert len(full["span_events"]) == 1


def test_event_endpoint_404s(client: TestClient) -> None:
    _many_events(client, 1)
    assert client.get(f"/v1/runs/{A}/events/{'9' * 16}").status_code == 404
    assert client.get(f"/v1/runs/{B}/events/{'0' * 16}").status_code == 404


@pytest.mark.parametrize(
    "params", [{"events_limit": 0}, {"events_limit": 1001}, {"events_offset": -1}]
)
def test_bad_paging_is_422(client: TestClient, params: dict[str, int]) -> None:
    _many_events(client, 1)
    assert client.get(f"/v1/runs/{A}", params=params).status_code == 422


def test_metadata_keys_with_dots_are_top_level_keys(client: TestClient) -> None:
    meta = {"a.b": "v", "a": {"b": "w"}, "k-1": "z"}
    client.post("/v1/spans", json={"spans": [run_span(A, "x", 1, meta)]})
    for query, expected in [("a.b:v", 1), ("a.b:w", 0), ("k-1:z", 1)]:
        assert client.get("/v1/runs", params={"meta": query}).json()["total"] == expected
