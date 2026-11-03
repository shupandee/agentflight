from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import agentflight
from agentflight import attributes as A


def by_kind(exporter: InMemorySpanExporter, kind: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.attributes.get(A.KIND) == kind]


def test_run_is_root_and_children_share_trace(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("demo", metadata={"user": "u1"}) as r:
        agentflight.decision("route", "search", reason="needs facts")
        agentflight.state_change("plan", ["search"], old=[])

    (run_span,) = by_kind(memory_exporter, A.RUN)
    assert run_span.parent is None
    assert format(run_span.context.trace_id, "032x") == r.run_id
    assert json.loads(run_span.attributes[A.RUN_METADATA]) == {"user": "u1"}
    assert run_span.status.status_code is StatusCode.OK

    (dec,) = by_kind(memory_exporter, A.DECISION)
    (state,) = by_kind(memory_exporter, A.STATE)
    for child in (dec, state):
        assert child.context.trace_id == run_span.context.trace_id
        assert child.parent.span_id == run_span.context.span_id
    assert json.loads(dec.attributes[A.DECISION_CHOICE]) == "search"
    assert dec.attributes[A.DECISION_REASON] == "needs facts"
    assert json.loads(state.attributes[A.STATE_NEW]) == ["search"]


def test_nested_runs_start_new_traces(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("outer") as outer, agentflight.run("inner") as inner:
        pass
    assert outer.run_id != inner.run_id


def test_llm_call_records_tokens_cost_and_latency(memory_exporter: InMemorySpanExporter) -> None:
    messages = [{"role": "user", "content": "hi"}]
    with agentflight.run("r"), agentflight.llm_call("toy-1", messages) as call:
        call.record("hello", tokens_in=3, tokens_out=1, cost_usd=0.0001)

    (span,) = by_kind(memory_exporter, A.LLM)
    attrs = span.attributes
    assert attrs[A.LLM_MODEL] == "toy-1"
    assert json.loads(attrs[A.LLM_MESSAGES]) == messages
    assert json.loads(attrs[A.LLM_RESPONSE]) == "hello"
    assert (attrs[A.LLM_TOKENS_IN], attrs[A.LLM_TOKENS_OUT]) == (3, 1)
    assert attrs[A.LLM_COST_USD] == pytest.approx(0.0001)
    assert span.end_time >= span.start_time


def test_tool_decorator_records_args_and_result(memory_exporter: InMemorySpanExporter) -> None:
    @agentflight.tool
    def add(a: int, b: int = 10) -> int:
        return a + b

    with agentflight.run("r"):
        assert add(1) == 11

    (span,) = by_kind(memory_exporter, A.TOOL)
    assert span.attributes[A.TOOL_NAME] == "add"
    assert json.loads(span.attributes[A.TOOL_ARGS]) == {"a": 1, "b": 10}
    assert json.loads(span.attributes[A.TOOL_RESULT]) == 11


def test_tool_error_marks_span_and_run(memory_exporter: InMemorySpanExporter) -> None:
    @agentflight.tool(name="flaky")
    def boom() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError), agentflight.run("r"):
        boom()

    (tool_span,) = by_kind(memory_exporter, A.TOOL)
    (run_span,) = by_kind(memory_exporter, A.RUN)
    assert tool_span.attributes[A.TOOL_NAME] == "flaky"
    assert tool_span.status.status_code is StatusCode.ERROR
    assert tool_span.attributes[A.ERROR_TYPE] == "RuntimeError"
    assert any(e.name == "exception" for e in tool_span.events)
    assert run_span.status.status_code is StatusCode.ERROR


def test_async_tool(memory_exporter: InMemorySpanExporter) -> None:
    @agentflight.tool
    async def fetch(q: str) -> str:
        await asyncio.sleep(0)
        return q.upper()

    async def main() -> str:
        with agentflight.run("r"):
            return await fetch("x")

    assert asyncio.run(main()) == "X"
    (span,) = by_kind(memory_exporter, A.TOOL)
    assert json.loads(span.attributes[A.TOOL_RESULT]) == "X"


def test_handled_error_does_not_fail_run(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("r"):
        try:
            raise ValueError("bad json")
        except ValueError as exc:
            agentflight.error(exc)

    (err,) = by_kind(memory_exporter, A.ERROR)
    (run_span,) = by_kind(memory_exporter, A.RUN)
    assert err.status.status_code is StatusCode.ERROR
    assert err.attributes[A.ERROR_MESSAGE] == "bad json"
    assert run_span.status.status_code is StatusCode.OK


def test_unserializable_values_fall_back_to_repr(memory_exporter: InMemorySpanExporter) -> None:
    class Thing:
        def __repr__(self) -> str:
            return "<Thing>"

    with agentflight.run("r"):
        agentflight.state_change("obj", Thing())
    (span,) = by_kind(memory_exporter, A.STATE)
    assert json.loads(span.attributes[A.STATE_NEW]) == "<Thing>"
