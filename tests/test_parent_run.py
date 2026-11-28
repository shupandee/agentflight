"""parent_run_id: sub-agent runs linked to the run that started them, end to end."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine

import agentflight
from agentflight import attributes as A
from agentflight.exporter import AgentFlightExporter
from agentflight.server import create_app
from agentflight.server.settings import Settings
from tests.test_otlp import JS, PB, _protobuf, _record

PARENT, CHILD, OTHER = "a" * 32, "b" * 32, "c" * 32


def run_spans(memory_exporter: InMemorySpanExporter) -> dict[str, Any]:
    return {
        s.attributes[A.RUN_NAME]: s
        for s in memory_exporter.get_finished_spans()
        if s.attributes.get(A.KIND) == A.RUN
    }


# SDK


def test_run_started_inside_a_run_is_linked_to_it(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("parent") as parent, agentflight.run("child") as child:
        pass
    spans = run_spans(memory_exporter)
    assert A.RUN_PARENT_RUN_ID not in spans["parent"].attributes
    assert spans["child"].attributes[A.RUN_PARENT_RUN_ID] == parent.run_id
    assert child.run_id != parent.run_id  # still its own trace
    # With no span open but the run's own, the child was started from the run span.
    assert spans["child"].attributes[A.RUN_PARENT_SPAN_ID] == format(
        spans["parent"].context.span_id, "016x"
    )


def test_child_records_the_span_it_was_started_from(
    memory_exporter: InMemorySpanExporter,
) -> None:
    @agentflight.tool
    def delegate() -> str:
        with agentflight.run("child"):
            return "done"

    with agentflight.run("parent"):
        delegate()
    spans = run_spans(memory_exporter)
    (tool,) = (
        s for s in memory_exporter.get_finished_spans() if s.attributes.get(A.KIND) == A.TOOL
    )
    assert spans["child"].attributes[A.RUN_PARENT_SPAN_ID] == format(tool.context.span_id, "016x")


def test_grandchildren_link_to_their_own_parent(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("a"), agentflight.run("b") as b, agentflight.run("c"):
        pass
    spans = run_spans(memory_exporter)
    assert spans["c"].attributes[A.RUN_PARENT_RUN_ID] == b.run_id


def test_sibling_and_later_runs_are_top_level(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("parent") as parent:
        with agentflight.run("one"):
            pass
        with agentflight.run("two"):
            pass
    with agentflight.run("after"):
        pass
    spans = run_spans(memory_exporter)
    assert spans["one"].attributes[A.RUN_PARENT_RUN_ID] == parent.run_id
    assert spans["two"].attributes[A.RUN_PARENT_RUN_ID] == parent.run_id
    assert A.RUN_PARENT_RUN_ID not in spans["after"].attributes


def test_explicit_parent_for_a_run_started_elsewhere(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agentflight.run("worker", parent_run_id=PARENT, parent_span_id="1" * 16):
        pass
    with agentflight.run("by-handle") as h:
        pass
    with agentflight.run("worker2", parent_run_id=h):
        pass
    spans = run_spans(memory_exporter)
    assert spans["worker"].attributes[A.RUN_PARENT_RUN_ID] == PARENT
    assert spans["worker"].attributes[A.RUN_PARENT_SPAN_ID] == "1" * 16
    assert spans["worker2"].attributes[A.RUN_PARENT_RUN_ID] == h.run_id
    assert A.RUN_PARENT_SPAN_ID not in spans["worker2"].attributes


def test_explicit_parent_wins_over_the_enclosing_run(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agentflight.run("enclosing"), agentflight.run("child", parent_run_id=PARENT):
        pass
    assert run_spans(memory_exporter)["child"].attributes[A.RUN_PARENT_RUN_ID] == PARENT


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 32, "a" * 31, 123])
def test_a_bad_parent_id_is_ignored_and_never_raises(
    memory_exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture, bad: Any
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="agentflight"),
        agentflight.run("child", parent_run_id=bad, parent_span_id="nope"),
    ):
        ran = True
    assert ran
    assert A.RUN_PARENT_RUN_ID not in run_spans(memory_exporter)["child"].attributes
    assert "parent_run_id" in caplog.text


def test_a_bad_parent_span_id_keeps_the_run_link(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agentflight.run("child", parent_run_id=PARENT, parent_span_id="zz"):
        pass
    attrs = run_spans(memory_exporter)["child"].attributes
    assert attrs[A.RUN_PARENT_RUN_ID] == PARENT
    assert A.RUN_PARENT_SPAN_ID not in attrs


def test_the_enclosing_run_is_restored_when_a_child_raises(
    memory_exporter: InMemorySpanExporter,
) -> None:
    with agentflight.run("parent") as parent:
        with pytest.raises(ValueError), agentflight.run("failing"):
            raise ValueError("boom")
        with agentflight.run("next"):
            pass
    spans = run_spans(memory_exporter)
    assert spans["next"].attributes[A.RUN_PARENT_RUN_ID] == parent.run_id


def test_the_link_does_not_leak_after_the_run(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("a"):
        pass
    with agentflight.run("b"):
        pass
    assert A.RUN_PARENT_RUN_ID not in run_spans(memory_exporter)["b"].attributes


# Server


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine, Settings(auth_disabled=True))) as c:
        yield c


def span(trace: str, sid: str, parent: str | None = None, **attrs: Any) -> dict[str, Any]:
    run_attrs = {A.KIND: A.RUN, A.RUN_NAME: f"run-{trace[0]}", **attrs}
    return {
        "trace_id": trace,
        "span_id": sid * 16,
        "parent_span_id": parent,
        "name": "run",
        "start_time_ns": 100,
        "end_time_ns": 200,
        "status": "ok",
        "attributes": run_attrs,
    }


def send(api: TestClient, *spans: dict[str, Any]) -> None:
    assert api.post("/v1/spans", json={"spans": list(spans)}).status_code == 202


def test_ingest_stores_the_parent_link(api: TestClient) -> None:
    send(
        api,
        span(PARENT, "1"),
        span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT, A.RUN_PARENT_SPAN_ID: "3" * 16}),
    )
    child = api.get(f"/v1/runs/{CHILD}").json()
    assert (child["parent_run_id"], child["parent_span_id"]) == (PARENT, "3" * 16)
    assert child["child_run_count"] == 0
    parent = api.get(f"/v1/runs/{PARENT}").json()
    assert parent["parent_run_id"] is None
    assert parent["child_run_count"] == 1


def test_the_child_can_arrive_before_its_parent(api: TestClient) -> None:
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}))
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_run_id"] == PARENT
    assert api.get(f"/v1/runs/{PARENT}").status_code == 404
    send(api, span(PARENT, "1"))
    assert api.get(f"/v1/runs/{PARENT}").json()["child_run_count"] == 1


def test_resending_the_run_span_is_idempotent_and_follows_it(api: TestClient) -> None:
    child = span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT})
    send(api, child, child)
    assert api.get("/v1/runs", params={"parent_run_id": PARENT}).json()["total"] == 1
    # Like the run's name and metadata, the link is whatever the run span last said.
    send(api, span(CHILD, "2"))
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_run_id"] is None


@pytest.mark.parametrize(
    "attrs",
    [
        {A.RUN_PARENT_RUN_ID: "not-a-run-id"},
        {A.RUN_PARENT_RUN_ID: 12345},
        {A.RUN_PARENT_RUN_ID: CHILD},  # itself
        {A.RUN_PARENT_RUN_ID: PARENT.upper()},
    ],
)
def test_malformed_or_self_parents_are_stored_as_no_parent(
    api: TestClient, attrs: dict[str, Any]
) -> None:
    send(api, span(CHILD, "2", **attrs, **{A.RUN_PARENT_SPAN_ID: "3" * 16}))
    body = api.get(f"/v1/runs/{CHILD}").json()
    assert (body["parent_run_id"], body["parent_span_id"]) == (None, None)


def test_a_malformed_parent_span_id_is_dropped_but_the_run_link_stays(api: TestClient) -> None:
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT, A.RUN_PARENT_SPAN_ID: "zz"}))
    body = api.get(f"/v1/runs/{CHILD}").json()
    assert (body["parent_run_id"], body["parent_span_id"]) == (PARENT, None)


def test_only_the_run_span_sets_the_link(api: TestClient) -> None:
    send(api, span(PARENT, "1"))
    llm = {
        **span(PARENT, "4", parent="1" * 16, **{A.RUN_PARENT_RUN_ID: OTHER}),
        "attributes": {A.KIND: A.LLM, A.RUN_PARENT_RUN_ID: OTHER},
    }
    send(api, llm)
    assert api.get(f"/v1/runs/{PARENT}").json()["parent_run_id"] is None


def test_filters(api: TestClient) -> None:
    other_child = "d" * 32
    send(
        api,
        span(PARENT, "1"),
        span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}),
        span(other_child, "5", **{A.RUN_PARENT_RUN_ID: OTHER}),
        span(OTHER, "6"),
    )

    def ids(**params: Any) -> set[str]:
        body = api.get("/v1/runs", params=params).json()
        assert body["total"] == len(body["runs"])
        return {r["id"] for r in body["runs"]}

    assert ids(parent_run_id=PARENT) == {CHILD}
    assert ids(parent_run_id=OTHER) == {other_child}
    assert ids(parent_run_id="e" * 32) == set()
    assert ids(has_parent="true") == {CHILD, other_child}
    assert ids(has_parent="false") == {PARENT, OTHER}
    assert ids(parent_run_id=PARENT, has_parent="false") == set()
    assert ids(parent_run_id=PARENT, status="ok") == {CHILD}
    assert api.get("/v1/runs", params={"parent_run_id": "nope"}).status_code == 422


def test_parent_deleted_leaves_the_child_with_its_link(api: TestClient) -> None:
    send(api, span(PARENT, "1"), span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}))
    assert api.delete(f"/v1/runs/{PARENT}").status_code == 204
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_run_id"] == PARENT
    page = api.get(f"/ui/runs/{CHILD}")
    assert page.status_code == 200
    assert "parent not recorded" in page.text
    assert f'href="/ui/runs/{PARENT}"' not in page.text  # no link to a page that 404s


def test_a_parent_cycle_cannot_hang_anything(api: TestClient) -> None:
    send(
        api,
        span(PARENT, "1", **{A.RUN_PARENT_RUN_ID: CHILD}),
        span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}),
    )
    for run_id in (PARENT, CHILD):
        assert api.get(f"/v1/runs/{run_id}").json()["child_run_count"] == 1
        assert api.get(f"/ui/runs/{run_id}").status_code == 200


# OTLP


def test_otlp_links_a_root_span_to_the_trace_it_links(api: TestClient) -> None:
    from opentelemetry.trace import Link, SpanKind

    holder: dict[str, Any] = {}

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("orchestrator") as orch:
            holder["orch"] = orch.get_span_context()
            link = Link(orch.get_span_context())
        with tracer.start_as_current_span("worker", kind=SpanKind.CONSUMER, links=[link]):
            pass

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    orch = holder["orch"]
    worker = next(s for s in spans if s.name == "worker")
    body = api.get(f"/v1/runs/{worker.context.trace_id:032x}").json()
    assert body["parent_run_id"] == f"{orch.trace_id:032x}"
    assert body["parent_span_id"] == f"{orch.span_id:016x}"
    assert api.get(f"/v1/runs/{orch.trace_id:032x}").json()["child_run_count"] == 1
    assert api.get("/v1/runs", params={"has_parent": "false"}).json()["total"] == 1


def test_otlp_ambiguous_or_same_trace_links_make_no_parent(api: TestClient) -> None:
    from opentelemetry.trace import Link

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("one") as one:
            l1 = Link(one.get_span_context())
        with tracer.start_as_current_span("two") as two:
            l2 = Link(two.get_span_context())
        with tracer.start_as_current_span("fan-in", links=[l1, l2]):
            pass
        with tracer.start_as_current_span("self-linked") as me:
            me.add_link(me.get_span_context())

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    for s in spans:
        body = api.get(f"/v1/runs/{s.context.trace_id:032x}").json()
        assert body["parent_run_id"] is None, s.name


def test_otlp_attribute_takes_precedence_over_links(api: TestClient) -> None:
    from opentelemetry.trace import Link

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("linked") as linked:
            link = Link(linked.get_span_context())
        with tracer.start_as_current_span("child", links=[link]) as child:
            child.set_attribute(A.RUN_PARENT_RUN_ID, PARENT)

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    child = next(s for s in spans if s.name == "child")
    assert api.get(f"/v1/runs/{child.context.trace_id:032x}").json()["parent_run_id"] == PARENT


def test_otlp_json_with_the_agentflight_attribute(api: TestClient) -> None:
    body = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": CHILD,
                                "spanId": "2" * 16,
                                "name": "sub-agent",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "200",
                                "attributes": [
                                    {
                                        "key": A.RUN_PARENT_RUN_ID,
                                        "value": {"stringValue": PARENT},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert api.post("/v1/traces", json=body, headers=JS).status_code == 200
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_run_id"] == PARENT


# SDK -> server -> viewer


@pytest.fixture
def ui(engine: Engine) -> Iterator[TestClient]:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)), follow_redirects=False)
    agentflight.configure(exporter=AgentFlightExporter("http://testserver", client=c), batch=False)
    yield c
    agentflight.shutdown()


def test_sdk_to_viewer(ui: TestClient) -> None:
    with agentflight.run("planner") as parent:
        with agentflight.run("researcher <b>x</b>") as child:
            agentflight.decision("pick", "web")
        with agentflight.run("writer") as other:
            pass
    agentflight.flush()

    got = ui.get(f"/v1/runs/{child.run_id}").json()
    assert got["parent_run_id"] == parent.run_id
    spawned_from = got["parent_span_id"]
    assert spawned_from
    # The parent's span the child was started from is a real event of the parent.
    parent_events = ui.get(f"/v1/runs/{parent.run_id}").json()["events"]
    assert spawned_from in {e["span_id"] for e in parent_events}

    kids = ui.get("/v1/runs", params={"parent_run_id": parent.run_id}).json()
    assert {r["id"] for r in kids["runs"]} == {child.run_id, other.run_id}
    tops = ui.get("/v1/runs", params={"has_parent": "false"}).json()
    assert [r["id"] for r in tops["runs"]] == [parent.run_id]

    page = ui.get(f"/ui/runs/{parent.run_id}").text
    assert "2 sub-agent runs" in page
    assert f"/ui/runs/{child.run_id}" in page and f"/ui/runs/{other.run_id}" in page
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page  # escaped
    assert "↳ sub-agent run" in page  # marked on the span that started it

    child_page = ui.get(f"/ui/runs/{child.run_id}").text
    assert "sub-agent run of" in child_page
    assert f"/ui/runs/{parent.run_id}" in child_page and "planner" in child_page

    table = ui.get("/ui/runs", params={"parent": parent.run_id}, headers={"HX-Request": "true"})
    assert f"/ui/runs/{child.run_id}" in table.text
    assert f'href="/ui/runs/{parent.run_id}"' in table.text  # the "sub-agent run of" link
    only_top = ui.get("/ui", params={"has_parent": "false"}).text
    assert f"/ui/runs/{parent.run_id}" in only_top
    assert f"/ui/runs/{child.run_id}" not in only_top
    only_child = ui.get("/ui", params={"has_parent": "true"}).text
    assert f"/ui/runs/{child.run_id}" in only_child
    assert f'href="/ui/runs/{parent.run_id}">planner' in only_top
    assert f'href="/ui/runs/{parent.run_id}">planner' not in only_child  # not a row


def test_viewer_rejects_a_bad_parent_filter(ui: TestClient) -> None:
    page = ui.get("/ui", params={"parent": "nope"})
    assert page.status_code == 200
    assert "Parent run must be a 32-character run id" in page.text


def test_viewer_has_parent_toggle_hides_child_runs(ui: TestClient) -> None:
    with agentflight.run("planner") as parent, agentflight.run("researcher") as child:
        pass
    page = ui.get("/ui").text
    assert '<select name="has_parent">' in page
    assert f"/ui/runs/{child.run_id}" in page
    hidden = ui.get("/ui/runs", params={"has_parent": "false"}, headers={"HX-Request": "true"})
    assert f'href="/ui/runs/{parent.run_id}">planner' in hidden.text
    assert f"/ui/runs/{child.run_id}" not in hidden.text
    assert '<option value="false" selected>' in ui.get("/ui", params={"has_parent": "false"}).text


# Rule 1: an explicit parent always wins over link inference


def _linked_otlp(api: TestClient, *, child_attrs: dict[str, Any]) -> tuple[Any, Any]:
    from opentelemetry.trace import Link

    holder: dict[str, Any] = {}

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("linked") as linked:
            holder["linked"] = linked.get_span_context()
            link = Link(linked.get_span_context())
        with tracer.start_as_current_span("child", links=[link]) as child:
            holder["child"] = child.get_span_context()
            child.set_attributes(child_attrs)

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    return holder["linked"], holder["child"]


def test_explicit_attribute_with_span_wins_over_links(api: TestClient) -> None:
    _, child = _linked_otlp(
        api, child_attrs={A.RUN_PARENT_RUN_ID: PARENT, A.RUN_PARENT_SPAN_ID: "9" * 16}
    )
    body = api.get(f"/v1/runs/{child.trace_id:032x}").json()
    assert (body["parent_run_id"], body["parent_span_id"]) == (PARENT, "9" * 16)
    assert body["parent_source"] == "explicit"


def test_sdk_spans_over_otlp_never_get_an_inferred_parent(api: TestClient) -> None:
    # AgentFlight SDK spans sent through an OTLP exporter say what they mean: no parent
    # attribute means a top-level run, whatever its links.
    _, child = _linked_otlp(api, child_attrs={A.KIND: A.RUN, A.RUN_NAME: "sdk-run"})
    body = api.get(f"/v1/runs/{child.trace_id:032x}").json()
    assert body["name"] == "sdk-run"
    assert (body["parent_run_id"], body["parent_source"]) == (None, None)


# Rule 2: the parent's source is stored, and the viewer words inferred links differently


def test_parent_source_is_explicit_or_inferred(api: TestClient) -> None:
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}))
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_source"] == "explicit"
    linked, child = _linked_otlp(api, child_attrs={})
    body = api.get(f"/v1/runs/{child.trace_id:032x}").json()
    assert body["parent_run_id"] == f"{linked.trace_id:032x}"
    assert body["parent_source"] == "inferred"
    listed = {r["id"]: r for r in api.get("/v1/runs").json()["runs"]}
    assert listed[f"{child.trace_id:032x}"]["parent_source"] == "inferred"
    assert listed[CHILD]["parent_source"] == "explicit"
    assert listed[f"{linked.trace_id:032x}"]["parent_source"] is None


def test_viewer_words_inferred_parents_as_started_from(api: TestClient) -> None:
    linked, child = _linked_otlp(api, child_attrs={})
    child_id, linked_id = f"{child.trace_id:032x}", f"{linked.trace_id:032x}"
    page = api.get(f"/ui/runs/{child_id}").text
    assert "started from run" in page and "sub-agent run of" not in page
    assert f'href="/ui/runs/{linked_id}"' in page
    listing = api.get("/ui").text
    assert "started from run" in listing and "sub-agent run of" not in listing
    parent_page = api.get(f"/ui/runs/{linked_id}").text
    assert "(started from this run, inferred)" in parent_page
    assert "run started from here" in parent_page
    # An explicit child keeps the sub-agent wording.
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: linked_id}))
    assert "sub-agent run of" in api.get(f"/ui/runs/{CHILD}").text


def test_a_resent_run_span_replaces_the_source(api: TestClient) -> None:
    send(
        api,
        span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT, A.RUN_PARENT_SOURCE: "inferred"}),
    )
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_source"] == "inferred"
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}))
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_source"] == "explicit"
    send(api, span(CHILD, "2"))
    assert api.get(f"/v1/runs/{CHILD}").json()["parent_source"] is None


# Rule 3: absent or deleted parents never fail ingest or the child


def test_runs_parent_run_id_has_no_foreign_key() -> None:
    from agentflight.server.models import Run

    assert not Run.__table__.c.parent_run_id.foreign_keys


def test_child_of_a_never_recorded_parent_loads(api: TestClient) -> None:
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT, A.RUN_PARENT_SPAN_ID: "3" * 16}))
    assert api.get(f"/v1/runs/{CHILD}").status_code == 200
    page = api.get(f"/ui/runs/{CHILD}")
    assert page.status_code == 200
    assert "parent not recorded" in page.text
    assert f'href="/ui/runs/{PARENT}"' not in page.text


def test_retention_expiring_a_parent_leaves_children_intact(api: TestClient) -> None:
    from agentflight.server import service

    old = {**span(PARENT, "1"), "start_time_ns": 100, "end_time_ns": 200}
    recent = {
        **span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}),
        "start_time_ns": 10_000,
        "end_time_ns": 20_000,
    }
    child_event = {
        **span(CHILD, "3", parent="2" * 16),
        "start_time_ns": 10_001,
        "end_time_ns": 10_002,
        "attributes": {A.KIND: A.TOOL, A.TOOL_NAME: "t"},
    }
    send(api, old, recent, child_event)
    with api.app.state.sessions() as session:  # type: ignore[attr-defined]
        assert service.cleanup_expired(session, cutoff_ns=1_000) == 1
    assert api.get(f"/v1/runs/{PARENT}").status_code == 404
    child = api.get(f"/v1/runs/{CHILD}").json()
    assert child["parent_run_id"] == PARENT
    assert child["event_count"] == 2
    assert "parent not recorded" in api.get(f"/ui/runs/{CHILD}").text


# Rule 4: self-links are rejected; cycles can't loop


def test_explicit_self_link_through_the_sdk_is_dropped(api: TestClient) -> None:
    # The SDK can't know its own run id up front; a sender that names it gets no parent.
    send(api, span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: CHILD}))
    body = api.get(f"/v1/runs/{CHILD}").json()
    assert (body["parent_run_id"], body["parent_source"]) == (None, None)
    assert api.get("/v1/runs", params={"parent_run_id": CHILD}).json()["total"] == 0


def test_filters_and_list_on_a_parent_cycle(api: TestClient) -> None:
    send(
        api,
        span(PARENT, "1", **{A.RUN_PARENT_RUN_ID: CHILD}),
        span(CHILD, "2", **{A.RUN_PARENT_RUN_ID: PARENT}),
    )
    assert [
        r["id"] for r in api.get("/v1/runs", params={"parent_run_id": PARENT}).json()["runs"]
    ] == [CHILD]
    assert [
        r["id"] for r in api.get("/v1/runs", params={"parent_run_id": CHILD}).json()["runs"]
    ] == [PARENT]
    assert api.get("/v1/runs", params={"has_parent": "false"}).json()["total"] == 0
    assert api.get("/v1/runs", params={"has_parent": "true"}).json()["total"] == 2
    for params in ({}, {"has_parent": "true"}, {"parent": PARENT}):
        assert api.get("/ui", params=params).status_code == 200


# Rule 5: link inference can be switched off


def test_otlp_link_inference_can_be_turned_off(engine: Engine) -> None:
    settings = Settings(auth_disabled=True, otlp_infer_parent_from_links=False)
    with TestClient(create_app(engine, settings)) as api:
        linked, child = _linked_otlp(api, child_attrs={})
        body = api.get(f"/v1/runs/{child.trace_id:032x}").json()
        assert (body["parent_run_id"], body["parent_source"]) == (None, None)
        # The links themselves are still kept on the event.
        root = next(e for e in body["events"] if e["span_id"] == f"{child.span_id:016x}")
        assert root["attributes"]["otel.links"][0]["trace_id"] == f"{linked.trace_id:032x}"
        # Explicit parents still work with inference off.
        _, explicit = _linked_otlp(api, child_attrs={A.RUN_PARENT_RUN_ID: PARENT})
        got = api.get(f"/v1/runs/{explicit.trace_id:032x}").json()
        assert (got["parent_run_id"], got["parent_source"]) == (PARENT, "explicit")


@pytest.mark.parametrize(("raw", "expected"), [(None, True), ("false", False), ("true", True)])
def test_link_inference_env_switch(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    if raw is None:
        monkeypatch.delenv("AGENTFLIGHT_OTLP_INFER_PARENT_FROM_LINKS", raising=False)
    else:
        monkeypatch.setenv("AGENTFLIGHT_OTLP_INFER_PARENT_FROM_LINKS", raw)
    monkeypatch.setenv("AGENTFLIGHT_AUTH_DISABLED", "true")
    assert Settings.from_env().otlp_infer_parent_from_links is expected


# parent_run_id is indexed


def test_parent_run_id_is_indexed(engine: Engine) -> None:
    from sqlalchemy import inspect

    indexes = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes("runs")}
    assert indexes["ix_runs_parent_run_id"] == ["parent_run_id"]
