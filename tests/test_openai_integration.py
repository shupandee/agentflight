from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import agentflight
from agentflight import attributes as A
from agentflight.integrations.openai import instrument_openai

MESSAGES = [{"role": "user", "content": "weather in Delhi?"}]
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]

COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-test",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Delhi"}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


class FakeCompletions:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response = response if response is not None else dict(COMPLETION)
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class AsyncFakeCompletions(FakeCompletions):
    async def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        return FakeCompletions.create(self, **kwargs)


def fake_client(completions: FakeCompletions) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def llm_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.attributes.get(A.KIND) == A.LLM]


def test_records_model_messages_params_response_and_usage(
    memory_exporter: InMemorySpanExporter,
) -> None:
    completions = FakeCompletions()
    client = instrument_openai(fake_client(completions))
    with agentflight.run("r"):
        resp = client.chat.completions.create(
            model="gpt-test", messages=MESSAGES, tools=TOOLS, temperature=0
        )

    assert resp is completions.response
    assert completions.calls == [
        {"model": "gpt-test", "messages": MESSAGES, "tools": TOOLS, "temperature": 0}
    ]
    (span,) = llm_spans(memory_exporter)
    attrs = span.attributes
    assert attrs[A.LLM_MODEL] == "gpt-test"
    assert attrs[A.INTEGRATION] == "openai"
    assert json.loads(attrs[A.LLM_MESSAGES]) == MESSAGES
    assert json.loads(attrs[A.LLM_PARAMS]) == {"tools": TOOLS, "temperature": 0}
    assert json.loads(attrs[A.LLM_RESPONSE]) == COMPLETION
    assert (attrs[A.LLM_TOKENS_IN], attrs[A.LLM_TOKENS_OUT]) == (11, 7)
    assert A.LLM_COST_USD not in attrs


def test_cost_function_is_used(memory_exporter: InMemorySpanExporter) -> None:
    seen: list[tuple[str, int | None, int | None]] = []

    def cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float:
        seen.append((model, tokens_in, tokens_out))
        return 0.5

    client = instrument_openai(fake_client(FakeCompletions()), cost=cost)
    client.chat.completions.create(model="gpt-test", messages=MESSAGES)
    assert seen == [("gpt-test", 11, 7)]
    assert llm_spans(memory_exporter)[0].attributes[A.LLM_COST_USD] == 0.5


def test_async_client(memory_exporter: InMemorySpanExporter) -> None:
    completions = AsyncFakeCompletions()
    client = instrument_openai(fake_client(completions))

    async def main() -> Any:
        with agentflight.run("r"):
            return await client.chat.completions.create(model="gpt-test", messages=MESSAGES)

    assert asyncio.run(main()) is completions.response
    (span,) = llm_spans(memory_exporter)
    assert span.attributes[A.LLM_TOKENS_IN] == 11


def test_transport_kwargs_are_not_recorded_and_redaction_applies() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(
        exporter=exporter, batch=False, redact=agentflight.redact_fields("content")
    )
    try:
        completions = FakeCompletions()
        client = instrument_openai(fake_client(completions))
        client.chat.completions.create(
            model="gpt-test",
            messages=MESSAGES,
            extra_headers={"Authorization": "Bearer sk-secret"},
            timeout=3,
        )
        # The client still gets every kwarg.
        assert completions.calls[0]["extra_headers"] == {"Authorization": "Bearer sk-secret"}
        (span,) = llm_spans(exporter)
        dumped = json.dumps(dict(span.attributes))
        assert "sk-secret" not in dumped
        assert A.LLM_PARAMS not in span.attributes
        assert json.loads(span.attributes[A.LLM_MESSAGES]) == [
            {"role": "user", "content": "[REDACTED]"}
        ]
    finally:
        agentflight.shutdown()


def test_streaming_is_passed_through_unrecorded(memory_exporter: InMemorySpanExporter) -> None:
    completions = FakeCompletions(response=iter(["chunk"]))
    client = instrument_openai(fake_client(completions))
    stream = client.chat.completions.create(model="gpt-test", messages=MESSAGES, stream=True)
    assert list(stream) == ["chunk"]
    assert llm_spans(memory_exporter) == []


def test_client_error_propagates_and_is_recorded(memory_exporter: InMemorySpanExporter) -> None:
    boom = ConnectionError("model down")
    client = instrument_openai(fake_client(FakeCompletions(exc=boom)))
    with pytest.raises(ConnectionError) as info:
        client.chat.completions.create(model="gpt-test", messages=MESSAGES)
    assert info.value is boom
    (span,) = llm_spans(memory_exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[A.ERROR_TYPE] == "ConnectionError"


class ExplodingUsage:
    @property
    def usage(self) -> Any:
        raise RuntimeError("bad usage")


def test_weird_response_and_broken_cost_never_break_the_call(
    memory_exporter: InMemorySpanExporter,
) -> None:
    def bad_cost(*_: Any) -> float:
        raise ValueError("no prices")

    resp = ExplodingUsage()
    client = instrument_openai(fake_client(FakeCompletions(response=resp)), cost=bad_cost)
    assert client.chat.completions.create(model="gpt-test", messages=MESSAGES) is resp
    client2 = instrument_openai(fake_client(FakeCompletions()), cost=bad_cost)
    assert client2.chat.completions.create(model="gpt-test", messages=MESSAGES)["id"] == "c1"


def test_broken_recorder_never_breaks_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    agentflight.configure(exporter=InMemorySpanExporter(), batch=False)
    try:

        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("tracer exploded")

        monkeypatch.setattr(agentflight.get_recorder().tracer, "start_as_current_span", broken)
        completions = FakeCompletions()
        client = instrument_openai(fake_client(completions))
        assert client.chat.completions.create(model="gpt-test", messages=MESSAGES) is (
            completions.response
        )
    finally:
        agentflight.shutdown()


def test_instrumenting_twice_is_a_noop_and_odd_objects_are_left_alone(
    memory_exporter: InMemorySpanExporter,
) -> None:
    client = fake_client(FakeCompletions())
    instrument_openai(instrument_openai(client))
    client.chat.completions.create(model="gpt-test", messages=MESSAGES)
    assert len(llm_spans(memory_exporter)) == 1

    odd = object()
    assert instrument_openai(odd) is odd


def test_real_openai_client_over_a_fake_transport(memory_exporter: InMemorySpanExporter) -> None:
    openai = pytest.importorskip("openai")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=COMPLETION)

    client = instrument_openai(
        openai.OpenAI(
            api_key="sk-not-a-real-key",
            base_url="http://fake.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    with agentflight.run("r"):
        resp = client.chat.completions.create(model="gpt-test", messages=MESSAGES, tools=TOOLS)

    assert resp.choices[0].message.tool_calls[0].function.name == "get_weather"
    assert len(requests) == 1
    (span,) = llm_spans(memory_exporter)
    recorded = json.loads(span.attributes[A.LLM_RESPONSE])
    assert recorded["choices"][0]["message"]["tool_calls"][0]["id"] == "t1"
    assert (span.attributes[A.LLM_TOKENS_IN], span.attributes[A.LLM_TOKENS_OUT]) == (11, 7)
    assert "sk-not-a-real-key" not in json.dumps(dict(span.attributes))
