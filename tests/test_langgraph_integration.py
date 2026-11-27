from __future__ import annotations

import json
import warnings
from typing import Any, TypedDict

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import agentflight
from agentflight import attributes as A

pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from agentflight.integrations.langgraph import AgentFlightCallbackHandler


class State(TypedDict):
    x: int


def add_one(state: State) -> dict[str, int]:
    return {"x": state["x"] + 1}


def double(state: State) -> dict[str, int]:
    if state["x"] > 5:
        raise ValueError("too big")
    return {"x": state["x"] * 2}


def counter_graph() -> Any:
    g = StateGraph(State)
    g.add_node("add_one", add_one)
    g.add_node("double", double)
    g.add_edge(START, "add_one")
    g.add_edge("add_one", "double")
    g.add_edge("double", END)
    return g.compile()


def spans(exporter: InMemorySpanExporter, kind: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.attributes.get(A.KIND) == kind]


def test_graph_becomes_a_run_with_node_spans(memory_exporter: InMemorySpanExporter) -> None:
    handler = AgentFlightCallbackHandler(run_name="counter", metadata={"v": 1})
    assert counter_graph().invoke({"x": 1}, config={"callbacks": [handler]}) == {"x": 4}

    (run,) = spans(memory_exporter, A.RUN)
    assert run.parent is None
    assert run.attributes[A.RUN_NAME] == "counter"
    assert json.loads(run.attributes[A.RUN_METADATA]) == {"v": 1}
    assert json.loads(run.attributes[A.NODE_INPUT]) == {"x": 1}
    assert json.loads(run.attributes[A.NODE_OUTPUT]) == {"x": 4}
    assert handler.run_id == format(run.context.trace_id, "032x")

    nodes = sorted(spans(memory_exporter, A.NODE), key=lambda s: s.start_time)
    assert [n.attributes[A.NODE_NAME] for n in nodes] == ["add_one", "double"]
    assert [n.attributes[A.NODE_STEP] for n in nodes] == [1, 2]
    assert [json.loads(n.attributes[A.NODE_INPUT]) for n in nodes] == [{"x": 1}, {"x": 2}]
    assert [json.loads(n.attributes[A.NODE_OUTPUT]) for n in nodes] == [{"x": 2}, {"x": 4}]
    for n in nodes:
        assert n.parent.span_id == run.context.span_id
        assert n.attributes[A.INTEGRATION] == "langgraph"


def test_graph_inside_an_agentflight_run_is_a_child(memory_exporter: InMemorySpanExporter) -> None:
    with agentflight.run("outer") as r:
        counter_graph().invoke({"x": 1}, config={"callbacks": [AgentFlightCallbackHandler()]})

    (run,) = spans(memory_exporter, A.RUN)
    assert format(run.context.trace_id, "032x") == r.run_id
    graph = next(s for s in spans(memory_exporter, A.NODE) if s.name.startswith("graph:"))
    assert graph.parent.span_id == run.context.span_id
    assert all(
        s.context.trace_id == run.context.trace_id for s in memory_exporter.get_finished_spans()
    )


def test_node_error_propagates_and_counts_once(memory_exporter: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="too big"):
        counter_graph().invoke({"x": 9}, config={"callbacks": [AgentFlightCallbackHandler()]})

    (run,) = spans(memory_exporter, A.RUN)
    failed = next(
        n for n in spans(memory_exporter, A.NODE) if n.attributes[A.NODE_NAME] == "double"
    )
    for span in (run, failed):
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes[A.ERROR_TYPE] == "ValueError"
    assert run.attributes[A.FAILURE_ID] == failed.attributes[A.FAILURE_ID]


class ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@tool
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return {"delhi": "31C, sunny"}[city.lower()]


def react_agent() -> Any:
    with warnings.catch_warnings():
        # Moved to langchain.agents in LangGraph 1.0; still the LangGraph-only prebuilt.
        warnings.simplefilter("ignore", DeprecationWarning)
        from langgraph.prebuilt import create_react_agent

    model = ToolCallingFake(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "get_weather", "args": {"city": "Delhi"}, "id": "t1"}],
                usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
            ),
            AIMessage(
                content="It is 31C and sunny in Delhi.",
                usage_metadata={"input_tokens": 20, "output_tokens": 9, "total_tokens": 29},
            ),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return create_react_agent(model, [get_weather])


def test_react_agent_records_llm_and_tool_calls(memory_exporter: InMemorySpanExporter) -> None:
    out = react_agent().invoke(
        {"messages": [("user", "Weather in Delhi?")]},
        config={"callbacks": [AgentFlightCallbackHandler(run_name="react")]},
    )
    assert out["messages"][-1].content == "It is 31C and sunny in Delhi."

    (run,) = spans(memory_exporter, A.RUN)
    nodes = {s.context.span_id: s for s in spans(memory_exporter, A.NODE)}
    llms = sorted(spans(memory_exporter, A.LLM), key=lambda s: s.start_time)
    assert [(s.attributes[A.LLM_TOKENS_IN], s.attributes[A.LLM_TOKENS_OUT]) for s in llms] == [
        (12, 5),
        (20, 9),
    ]
    first_response = json.loads(llms[0].attributes[A.LLM_RESPONSE])
    assert first_response["tool_calls"][0]["args"] == {"city": "Delhi"}
    assert json.loads(llms[0].attributes[A.LLM_MESSAGES])[-1] == {
        "type": "human",
        "content": "Weather in Delhi?",
    }
    # Model calls hang off the "agent" node, not LangChain's internal runnables.
    for s in llms:
        assert nodes[s.parent.span_id].attributes[A.NODE_NAME] == "agent"

    (t,) = spans(memory_exporter, A.TOOL)
    assert t.attributes[A.TOOL_NAME] == "get_weather"
    assert json.loads(t.attributes[A.TOOL_ARGS]) == {"city": "Delhi"}
    assert json.loads(t.attributes[A.TOOL_RESULT])["content"] == "31C, sunny"
    assert nodes[t.parent.span_id].attributes[A.NODE_NAME] == "tools"
    assert all(
        s.context.trace_id == run.context.trace_id for s in memory_exporter.get_finished_spans()
    )


def test_redaction_applies_to_graph_payloads() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(exporter=exporter, batch=False, redact=agentflight.redact_fields("x"))
    try:
        counter_graph().invoke({"x": 1}, config={"callbacks": [AgentFlightCallbackHandler()]})
        for s in spans(exporter, A.NODE):
            assert json.loads(s.attributes[A.NODE_INPUT]) == {"x": "[REDACTED]"}
    finally:
        agentflight.shutdown()


def test_broken_recorder_never_breaks_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    agentflight.configure(exporter=InMemorySpanExporter(), batch=False)
    try:

        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("tracer exploded")

        monkeypatch.setattr(agentflight.get_recorder().tracer, "start_span", broken)
        handler = AgentFlightCallbackHandler()
        assert counter_graph().invoke({"x": 1}, config={"callbacks": [handler]}) == {"x": 4}
        out = react_agent().invoke(
            {"messages": [("user", "Weather in Delhi?")]}, config={"callbacks": [handler]}
        )
        assert out["messages"][-1].content == "It is 31C and sunny in Delhi."
        with pytest.raises(ValueError, match="too big"):
            counter_graph().invoke({"x": 9}, config={"callbacks": [handler]})
    finally:
        agentflight.shutdown()


def test_handler_methods_swallow_bad_input(memory_exporter: InMemorySpanExporter) -> None:
    from uuid import uuid4

    handler = AgentFlightCallbackHandler()
    rid = uuid4()
    handler.on_llm_end(object(), run_id=rid)
    handler.on_chain_end(object(), run_id=rid)
    handler.on_tool_error(RuntimeError("x"), run_id=rid)
    handler.on_chat_model_start(None, [], run_id=rid)  # type: ignore[arg-type]


def test_graph_run_is_stored_with_totals(recorded: TestClient) -> None:
    handler = AgentFlightCallbackHandler(run_name="react")
    react_agent().invoke(
        {"messages": [("user", "Weather in Delhi?")]}, config={"callbacks": [handler]}
    )
    agentflight.flush()

    run = recorded.get(f"/v1/runs/{handler.run_id}").json()
    assert run["status"] == "ok"
    assert (run["llm_calls"], run["tool_calls"]) == (2, 1)
    assert (run["tokens_in"], run["tokens_out"]) == (32, 14)
    node = next(e for e in run["events"] if e["kind"] == "node")
    assert isinstance(node["attributes"][A.NODE_INPUT], dict)
