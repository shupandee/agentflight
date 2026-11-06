"""Explicit truncation and the redaction hook."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agentflight
from agentflight import attributes as A


def _only(exporter: InMemorySpanExporter, kind: str) -> Any:
    (span,) = [s for s in exporter.get_finished_spans() if s.attributes[A.KIND] == kind]
    return span


def test_default_limit_is_32000_and_truncation_is_recorded() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(exporter=exporter, batch=False)
    big = "x" * 40_000
    with agentflight.run("r"), agentflight.llm_call("m", [big]) as call:
        call.record("short")
    span = _only(exporter, A.LLM)
    stored = span.attributes[A.LLM_MESSAGES]
    original = json.dumps([big])
    assert stored == original[:32_000]
    info = json.loads(span.attributes[A.TRUNCATED])
    assert info == {
        A.LLM_MESSAGES: {
            "original_length": len(original),
            "stored_length": 32_000,
            "reason": "exceeds max_attr_chars=32000",
        }
    }
    agentflight.shutdown()


def test_custom_limit_and_late_payloads_merge_truncation_info() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(exporter=exporter, batch=False, max_attr_chars=10)

    @agentflight.tool
    def echo(text: str) -> str:
        return text

    with agentflight.run("r"):
        with agentflight.llm_call("m", ["a" * 20]) as call:
            call.record("b" * 20)
        echo("c" * 20)
    llm = _only(exporter, A.LLM)
    assert set(json.loads(llm.attributes[A.TRUNCATED])) == {A.LLM_MESSAGES, A.LLM_RESPONSE}
    assert len(llm.attributes[A.LLM_RESPONSE]) == 10
    tool = _only(exporter, A.TOOL)
    assert set(json.loads(tool.attributes[A.TRUNCATED])) == {A.TOOL_ARGS, A.TOOL_RESULT}
    agentflight.shutdown()


def test_small_payloads_are_not_marked_and_limit_can_be_disabled() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(exporter=exporter, batch=False, max_attr_chars=None)
    with agentflight.run("r"):
        agentflight.state_change("s", "y" * 50_000)
    span = _only(exporter, A.STATE)
    assert A.TRUNCATED not in span.attributes
    assert len(span.attributes[A.STATE_NEW]) == 50_002
    agentflight.shutdown()


def test_redact_fields_masks_nested_keys_everywhere() -> None:
    exporter = InMemorySpanExporter()
    agentflight.configure(
        exporter=exporter, batch=False, redact=agentflight.redact_fields("api_key", "Password")
    )

    @agentflight.tool
    def login(user: str, password: str) -> dict[str, Any]:
        return {"token": "t", "api_key": "sk-live"}

    with agentflight.run("r", metadata={"cfg": {"API_KEY": "sk-meta"}}):
        with agentflight.llm_call("m", [{"role": "user", "content": "hi", "api_key": "sk-msg"}]):
            pass
        login("me", "hunter2")
    dumped = json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()])
    for secret in ("sk-live", "sk-meta", "sk-msg", "hunter2"):
        assert secret not in dumped
    assert json.loads(_only(exporter, A.TOOL).attributes[A.TOOL_ARGS]) == {
        "user": "me",
        "password": "[REDACTED]",
    }
    agentflight.shutdown()


def test_custom_hook_gets_attribute_key_and_failing_hook_fails_closed() -> None:
    exporter = InMemorySpanExporter()
    seen: list[str] = []

    def hook(key: str, value: Any) -> Any:
        seen.append(key)
        if key == A.STATE_NEW:
            raise RuntimeError("hook bug")
        return value

    agentflight.configure(exporter=exporter, batch=False, redact=hook)
    with agentflight.run("r"):
        agentflight.state_change("secret", "sensitive-value", old="x")
    span = _only(exporter, A.STATE)
    assert json.loads(span.attributes[A.STATE_NEW]) == "<redaction failed>"
    assert "sensitive-value" not in json.dumps(dict(span.attributes))
    assert A.RUN_METADATA in seen and A.STATE_OLD in seen
    agentflight.shutdown()


def test_server_exposes_truncation(client: TestClient) -> None:
    exporter = agentflight.AgentFlightExporter("http://testserver", client=client)
    agentflight.configure(exporter=exporter, batch=False, max_attr_chars=8)
    with agentflight.run("r") as r:
        agentflight.decision("d", "z" * 100)
    agentflight.shutdown()
    events = client.get(f"/v1/runs/{r.run_id}").json()["events"]
    decision = next(e for e in events if e["kind"] == "decision")
    assert decision["truncated"][A.DECISION_CHOICE]["original_length"] == 102
    assert decision["attributes"][A.DECISION_CHOICE] == '"zzzzzzz'
    run = next(e for e in events if e["kind"] == "run")
    assert run["truncated"] == {}
