"""Exception messages and stack traces go through the redact hook."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agentflight
from agentflight import attributes as A
from agentflight.sdk import REDACTION_FAILED

SECRET = "sk-test-SECRET-123"


def scrub(key: str, value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(SECRET, "[KEY]")
    return value


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    agentflight.configure(exporter=exp, batch=False, redact=scrub)
    yield exp
    agentflight.shutdown()


def _fail() -> None:
    secret = SECRET
    raise RuntimeError(f"auth failed for key {secret}")


def _everything(exp: InMemorySpanExporter) -> str:
    parts = []
    for span in exp.get_finished_spans():
        parts.append(json.dumps(dict(span.attributes or {})))
        parts.append(span.status.description or "")
        for event in span.events:
            parts.append(json.dumps(dict(event.attributes or {})))
    return "\n".join(parts)


def test_secret_in_exception_is_redacted_everywhere(exporter: InMemorySpanExporter) -> None:
    @agentflight.tool
    def call_api() -> None:
        _fail()

    with pytest.raises(RuntimeError), agentflight.run("r"):
        call_api()
    text = _everything(exporter)
    assert SECRET not in text
    assert "auth failed for key [KEY]" in text

    tool = next(s for s in exporter.get_finished_spans() if s.name.startswith("tool"))
    assert tool.attributes[A.ERROR_MESSAGE] == "auth failed for key [KEY]"
    assert tool.status.description == "RuntimeError: auth failed for key [KEY]"
    (event,) = tool.events
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "RuntimeError"
    assert event.attributes["exception.message"] == "auth failed for key [KEY]"
    assert "Traceback (most recent call last)" in event.attributes["exception.stacktrace"]
    assert "_fail" in event.attributes["exception.stacktrace"]


def test_hook_sees_error_keys() -> None:
    seen: list[str] = []

    def hook(key: str, value: Any) -> Any:
        seen.append(key)
        return value

    exp = InMemorySpanExporter()
    agentflight.configure(exporter=exp, batch=False, redact=hook)
    try:
        with pytest.raises(RuntimeError), agentflight.run("r"):
            _fail()
    finally:
        agentflight.shutdown()
    assert {A.ERROR_MESSAGE, "exception.stacktrace"} <= set(seen)


def test_failing_hook_fails_closed_for_errors() -> None:
    def broken(key: str, value: Any) -> Any:
        if key in (A.ERROR_MESSAGE, "exception.stacktrace"):
            raise ValueError("hook bug")
        return value

    exp = InMemorySpanExporter()
    agentflight.configure(exporter=exp, batch=False, redact=broken)
    try:
        with pytest.raises(RuntimeError, match="auth failed"), agentflight.run("r"):
            _fail()
    finally:
        agentflight.shutdown()
    (run,) = exp.get_finished_spans()
    assert SECRET not in _everything(exp)
    assert run.attributes[A.ERROR_MESSAGE] == REDACTION_FAILED
    assert run.events[0].attributes["exception.stacktrace"] == REDACTION_FAILED


def test_long_error_messages_are_capped_and_marked() -> None:
    exp = InMemorySpanExporter()
    agentflight.configure(exporter=exp, batch=False, max_attr_chars=50)
    try:
        with pytest.raises(ValueError), agentflight.run("r"):
            raise ValueError("x" * 200)
    finally:
        agentflight.shutdown()
    (run,) = exp.get_finished_spans()
    assert run.attributes[A.ERROR_MESSAGE] == "x" * 50
    truncated = json.loads(run.attributes[A.TRUNCATED])
    assert truncated[A.ERROR_MESSAGE]["original_length"] == 200
    assert len(run.events[0].attributes["exception.stacktrace"]) == 50


def test_exception_type_matches_otel_naming(memory_exporter: InMemorySpanExporter) -> None:
    class CustomError(Exception):
        pass

    with pytest.raises(CustomError), agentflight.run("r"):
        raise CustomError("boom")
    (run,) = memory_exporter.get_finished_spans()
    assert run.events[0].attributes["exception.type"] == (
        f"{__name__}.test_exception_type_matches_otel_naming.<locals>.CustomError"
    )
    assert run.attributes[A.ERROR_TYPE] == "CustomError"
