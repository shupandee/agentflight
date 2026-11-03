"""Recording API: runs, LLM calls, tool calls, decisions, state changes and errors.

Every recorded item becomes an OpenTelemetry span carrying ``agentflight.*`` attributes.
A run is the root span; its trace id is the run id.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar, overload

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Status, StatusCode

from agentflight import attributes as A
from agentflight.exporter import AgentFlightExporter

P = ParamSpec("P")
R = TypeVar("R")

MAX_ATTR_CHARS = 32_000


def _dump(value: Any) -> str:
    """Serialize a value to a JSON string for span attributes (truncated if huge)."""
    try:
        text = json.dumps(value, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(repr(value))
    if len(text) > MAX_ATTR_CHARS:
        text = text[:MAX_ATTR_CHARS] + "...<truncated>"
    return text


def _record_error(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_attribute(A.ERROR_TYPE, type(exc).__name__)
    span.set_attribute(A.ERROR_MESSAGE, str(exc))
    span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


class Recorder:
    """Owns a private TracerProvider so AgentFlight never clobbers app-wide OTel config."""

    def __init__(self, exporter: SpanExporter | None = None, *, batch: bool = True) -> None:
        self.provider = TracerProvider()
        if exporter is not None:
            processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
            self.provider.add_span_processor(processor)
        self.tracer = self.provider.get_tracer("agentflight")

    def flush(self, timeout_millis: int = 30_000) -> bool:
        return bool(self.provider.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self.provider.shutdown()


_recorder: Recorder = Recorder()


def configure(
    endpoint: str = "http://localhost:8000",
    *,
    exporter: SpanExporter | None = None,
    batch: bool = True,
) -> Recorder:
    """Point the SDK at an AgentFlight server (or a custom exporter) and return the recorder."""
    global _recorder
    _recorder.shutdown()
    _recorder = Recorder(exporter or AgentFlightExporter(endpoint), batch=batch)
    return _recorder


def get_recorder() -> Recorder:
    return _recorder


def flush(timeout_millis: int = 30_000) -> bool:
    """Block until all finished spans have been exported."""
    return _recorder.flush(timeout_millis)


def shutdown() -> None:
    _recorder.shutdown()


@dataclass
class RunHandle:
    span: Span
    name: str

    @property
    def run_id(self) -> str:
        return format(self.span.get_span_context().trace_id, "032x")


@contextmanager
def run(name: str, *, metadata: dict[str, Any] | None = None) -> Iterator[RunHandle]:
    """Record one agent run. Everything recorded inside becomes part of it.

    Runs always start a new trace, even when nested inside another span.
    """
    with _recorder.tracer.start_as_current_span(
        f"run:{name}",
        context=trace.set_span_in_context(trace.INVALID_SPAN),
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            A.KIND: A.RUN,
            A.RUN_NAME: name,
            A.RUN_METADATA: _dump(metadata or {}),
        },
    ) as span:
        try:
            yield RunHandle(span=span, name=name)
        except BaseException as exc:
            _record_error(span, exc)
            raise
        span.set_status(Status(StatusCode.OK))


@dataclass
class LLMCall:
    """Handle yielded by :func:`llm_call`; fill in the response once you have it."""

    span: Span
    model: str
    response: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    _start: float = field(default_factory=time.perf_counter)

    def record(
        self,
        response: Any,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.response = response
        self.span.set_attribute(A.LLM_RESPONSE, _dump(response))
        if tokens_in is not None:
            self.tokens_in = tokens_in
            self.span.set_attribute(A.LLM_TOKENS_IN, tokens_in)
        if tokens_out is not None:
            self.tokens_out = tokens_out
            self.span.set_attribute(A.LLM_TOKENS_OUT, tokens_out)
        if cost_usd is not None:
            self.cost_usd = cost_usd
            self.span.set_attribute(A.LLM_COST_USD, cost_usd)


@contextmanager
def llm_call(
    model: str,
    messages: Any,
    *,
    name: str | None = None,
    params: dict[str, Any] | None = None,
) -> Iterator[LLMCall]:
    """Record a model call. Latency is the span duration.

    Usage::

        with agentflight.llm_call("gpt-4o-mini", messages) as call:
            resp = client.chat(...)
            call.record(resp.text, tokens_in=..., tokens_out=..., cost_usd=...)
    """
    attrs: dict[str, Any] = {A.KIND: A.LLM, A.LLM_MODEL: model, A.LLM_MESSAGES: _dump(messages)}
    if params:
        attrs["agentflight.llm.params"] = _dump(params)
    with _recorder.tracer.start_as_current_span(
        name or f"llm:{model}",
        attributes=attrs,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        call = LLMCall(span=span, model=model)
        try:
            yield call
        except BaseException as exc:
            _record_error(span, exc)
            raise
        span.set_status(Status(StatusCode.OK))


def _bind_args(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": kwargs}


@contextmanager
def _tool_span(tool_name: str, call_args: Any) -> Iterator[Span]:
    with _recorder.tracer.start_as_current_span(
        f"tool:{tool_name}",
        attributes={A.KIND: A.TOOL, A.TOOL_NAME: tool_name, A.TOOL_ARGS: _dump(call_args)},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            _record_error(span, exc)
            raise
        span.set_status(Status(StatusCode.OK))


@overload
def tool(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def tool(*, name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(
    func: Callable[P, R] | None = None, /, *, name: str | None = None
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator recording each call of a tool: name, arguments, result or error.

    Works on sync and async functions: ``@agentflight.tool`` or ``@agentflight.tool(name=...)``.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        tool_name = name or fn.__name__

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with _tool_span(tool_name, _bind_args(fn, args, kwargs)) as span:
                    result = await fn(*args, **kwargs)
                    span.set_attribute(A.TOOL_RESULT, _dump(result))
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with _tool_span(tool_name, _bind_args(fn, args, kwargs)) as span:
                result = fn(*args, **kwargs)
                span.set_attribute(A.TOOL_RESULT, _dump(result))
                return result

        return wrapper

    if func is not None:
        return decorate(func)
    return decorate


def decision(name: str, choice: Any, *, reason: str | None = None) -> None:
    """Record a decision point (e.g. which tool the agent chose and why)."""
    attrs: dict[str, Any] = {
        A.KIND: A.DECISION,
        A.DECISION_NAME: name,
        A.DECISION_CHOICE: _dump(choice),
    }
    if reason is not None:
        attrs[A.DECISION_REASON] = reason
    span = _recorder.tracer.start_span(f"decision:{name}", attributes=attrs)
    span.set_status(Status(StatusCode.OK))
    span.end()


def state_change(key: str, new: Any, *, old: Any = None) -> None:
    """Record a change to agent state (memory, plan, scratchpad...)."""
    span = _recorder.tracer.start_span(
        f"state:{key}",
        attributes={
            A.KIND: A.STATE,
            A.STATE_KEY: key,
            A.STATE_OLD: _dump(old),
            A.STATE_NEW: _dump(new),
        },
    )
    span.set_status(Status(StatusCode.OK))
    span.end()


def error(exc: BaseException, *, name: str | None = None) -> None:
    """Record a handled error that did not propagate out of the run."""
    span = _recorder.tracer.start_span(
        name or f"error:{type(exc).__name__}", attributes={A.KIND: A.ERROR}
    )
    _record_error(span, exc)
    span.end()
