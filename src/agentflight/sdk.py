"""Recording API: runs, LLM calls, tool calls, decisions, state changes and errors.

Every recorded item becomes an OpenTelemetry span carrying ``agentflight.*`` attributes.
A run is the root span; its trace id is the run id.
"""

from __future__ import annotations

import atexit
import functools
import inspect
import json
import logging
import os
import threading
import time
import traceback
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar, overload

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Status, StatusCode

from agentflight import attributes as A
from agentflight._queue import DropCountingBatchProcessor
from agentflight.exporter import DEFAULT_MAX_ATTEMPTS, AgentFlightExporter

logger = logging.getLogger("agentflight")

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_MAX_ATTR_CHARS = 32_000
REDACTION_FAILED = "<redaction failed>"
EXCEPTION_STACKTRACE = "exception.stacktrace"

Redactor = Callable[[str, Any], Any]

# The active replay session (see agentflight.replayer), or None when recording normally.
# Kept here so the SDK needs no import of the replayer; it only calls ``session.next``.
_replay_session: ContextVar[Any] = ContextVar("agentflight_replay_session", default=None)


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a piece of instrumentation; never let it raise into agent code."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("agentflight: instrumentation error (ignored)", exc_info=True)
        return None


DEFAULT_SHUTDOWN_MILLIS = 30_000


def _bounded(fn: Callable[..., Any], timeout_millis: float, *args: Any) -> Any:
    """Run ``fn`` (fail-safe) and wait at most ``timeout_millis`` for it.

    OpenTelemetry's own flush and shutdown timeouts aren't reliable across versions (1.45's
    ``force_flush`` ignores its timeout and exports the whole queue), so the wait is bounded
    here. If time runs out, ``fn`` keeps going on a daemon thread and None is returned.
    """
    result: list[Any] = []
    worker = threading.Thread(
        target=lambda: result.append(_safe(fn, *args)), name="agentflight-wait", daemon=True
    )
    try:
        worker.start()
        worker.join(max(0.0, timeout_millis / 1000))
    except Exception:
        logger.warning("agentflight: could not wait for %r (ignored)", fn, exc_info=True)
        return None
    return result[0] if result else None


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _dump(value: Any) -> str:
    """Serialize a value to a JSON string. Never raises.

    Anything json can't handle falls back to repr, and a failing repr falls back to a type
    placeholder.
    """
    try:
        return json.dumps(value, default=_safe_repr, ensure_ascii=False)
    except Exception:
        return json.dumps(_safe_repr(value))


@dataclass(frozen=True)
class _Payload:
    """Marks a user-supplied value that must be redacted, serialized and size-limited."""

    value: Any


def _redact(key: str, value: Any) -> Any:
    hook = _recorder.redact
    if hook is None:
        return value
    try:
        return hook(key, value)
    except Exception:
        # Fail closed: if the hook breaks, don't record the possibly-sensitive value.
        logger.warning("agentflight: redact hook failed for %s (value dropped)", key)
        return REDACTION_FAILED


def _encode(key: str, value: Any) -> tuple[str, dict[str, Any] | None]:
    """Redact, serialize and truncate one payload. Returns (text, truncation info)."""
    text = _dump(_redact(key, value))
    limit = _recorder.max_attr_chars
    if limit is None or len(text) <= limit:
        return text, None
    info = {
        "original_length": len(text),
        "stored_length": limit,
        "reason": f"exceeds max_attr_chars={limit}",
    }
    return text[:limit], info


def _finalize(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    truncated: dict[str, Any] = {}
    for key, value in attrs.items():
        if isinstance(value, _Payload):
            out[key], info = _encode(key, value.value)
            if info:
                truncated[key] = info
        else:
            out[key] = value
    if truncated:
        out[A.TRUNCATED] = json.dumps(truncated)
    return out


def _note_truncation(span: Span, key: str, info: dict[str, Any] | None) -> None:
    if info:
        existing: dict[str, Any] = {}
        current = getattr(span, "attributes", None) or {}
        if A.TRUNCATED in current:
            existing = json.loads(current[A.TRUNCATED])
        existing[key] = info
        span.set_attribute(A.TRUNCATED, json.dumps(existing))


def _set_payload(span: Span, key: str, value: Any) -> None:
    text, info = _encode(key, value)
    span.set_attribute(key, text)
    _note_truncation(span, key, info)


def _redact_text(key: str, text: str) -> tuple[str, dict[str, Any] | None]:
    """Redact and size-limit plain text (exception messages, stack traces).

    Unlike payloads it's stored as the text itself, not JSON. A hook that returns a non-string
    gets its result JSON-encoded; a failing hook fails closed like it does for payloads.
    """
    redacted = _redact(key, text)
    out = redacted if isinstance(redacted, str) else _dump(redacted)
    limit = _recorder.max_attr_chars
    if limit is None or len(out) <= limit:
        return out, None
    info = {
        "original_length": len(out),
        "stored_length": limit,
        "reason": f"exceeds max_attr_chars={limit}",
    }
    return out[:limit], info


def redact_fields(*names: str, replacement: str = "[REDACTED]") -> Redactor:
    """Build a redact hook that masks dict entries with these names, at any depth.

    Matching is case-insensitive. Use it as ``configure(redact=redact_fields("api_key"))``
    or compose it into your own hook.
    """
    lowered = {n.lower() for n in names}

    def scrub(value: Any, depth: int = 0) -> Any:
        if depth > 50:
            return value
        if isinstance(value, dict):
            return {
                k: replacement if str(k).lower() in lowered else scrub(v, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [scrub(v, depth + 1) for v in value]
        return value

    def hook(key: str, value: Any) -> Any:
        return scrub(value)

    return hook


def _exc_text(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


_FAILURE_ATTR = "__agentflight_failure_id__"
# Fallback for exception types that reject new attributes (e.g. __slots__).
_failure_ids: weakref.WeakKeyDictionary[BaseException, str] = weakref.WeakKeyDictionary()


def _get_failure_id(exc: BaseException) -> str | None:
    fid = getattr(exc, _FAILURE_ATTR, None)
    if isinstance(fid, str):
        return fid
    try:
        return _failure_ids.get(exc)
    except TypeError:
        return None


def failure_id(exc: BaseException) -> str:
    """Return the failure id for an exception, assigning one on first sight.

    The id follows the exception object, so a tool exception, the agent's
    ``agentflight.error(exc)`` of that same object and the run failing with it all share
    one id. An exception raised while handling (or ``from``) an already-recorded failure
    inherits that failure's id, so wrapping and re-raising counts once.
    """
    fid = _get_failure_id(exc)
    if fid is None:
        seen: set[int] = set()
        cause = exc.__cause__ or exc.__context__
        while cause is not None and id(cause) not in seen and fid is None:
            seen.add(id(cause))
            fid = _get_failure_id(cause)
            cause = cause.__cause__ or cause.__context__
        fid = fid or uuid.uuid4().hex
        try:
            setattr(exc, _FAILURE_ATTR, fid)
        except Exception:
            with suppress(TypeError):
                _failure_ids[exc] = fid
    return fid


def _stacktrace(exc: BaseException) -> str:
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return f"<unformattable traceback for {type(exc).__name__}>"


def _record_error(span: Span, exc: BaseException) -> None:
    """Record an exception on a span. The message and stack trace go through the ``redact``
    hook (keys ``agentflight.error.message`` and ``exception.stacktrace``) and are capped at
    ``max_attr_chars``, like payloads; nothing unredacted is stored."""
    error_type = type(exc).__name__
    message, info = _redact_text(A.ERROR_MESSAGE, _exc_text(exc))
    span.set_attribute(A.FAILURE_ID, failure_id(exc))
    stack, _ = _redact_text(EXCEPTION_STACKTRACE, _stacktrace(exc))
    # OpenTelemetry's exception event (semantic convention names), built here instead of
    # span.record_exception() so the message and stack trace are redacted first.
    module, qualname = type(exc).__module__, type(exc).__qualname__
    _safe(
        span.add_event,
        "exception",
        {
            "exception.type": f"{module}.{qualname}" if module != "builtins" else qualname,
            "exception.message": message,
            EXCEPTION_STACKTRACE: stack,
            "exception.escaped": "False",
        },
    )
    span.set_attribute(A.ERROR_TYPE, error_type)
    span.set_attribute(A.ERROR_MESSAGE, message)
    _note_truncation(span, A.ERROR_MESSAGE, info)
    span.set_status(Status(StatusCode.ERROR, f"{error_type}: {message}"))


DEFAULT_MAX_QUEUE_SIZE = 2048  # OpenTelemetry's default for the batch span processor
_DEFAULT_EXPORT_BATCH = 512


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("agentflight: ignoring %s=%r (not a positive integer)", name, raw)
        return None
    return value


def resolve_queue_size(max_queue_size: int | None = None) -> int:
    """The batch queue size: the argument, else ``AGENTFLIGHT_MAX_QUEUE_SIZE``, else
    OpenTelemetry's ``OTEL_BSP_MAX_QUEUE_SIZE``, else 2048."""
    if max_queue_size is not None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        return max_queue_size
    return (
        _env_int("AGENTFLIGHT_MAX_QUEUE_SIZE")
        or _env_int("OTEL_BSP_MAX_QUEUE_SIZE")
        or DEFAULT_MAX_QUEUE_SIZE
    )


def _export_batch_size() -> int:
    return _env_int("OTEL_BSP_MAX_EXPORT_BATCH_SIZE") or _DEFAULT_EXPORT_BATCH


class Recorder:
    """Owns a private TracerProvider so AgentFlight never clobbers app-wide OTel config."""

    def __init__(
        self,
        exporter: SpanExporter | None = None,
        *,
        batch: bool = True,
        max_attr_chars: int | None = DEFAULT_MAX_ATTR_CHARS,
        redact: Redactor | None = None,
        max_queue_size: int | None = None,
        run_span_deadline_s: float = 5.0,
    ) -> None:
        self.max_attr_chars = max_attr_chars
        self.redact = redact
        self.max_queue_size = resolve_queue_size(max_queue_size)
        self._queue: DropCountingBatchProcessor | None = None
        # OTel's own atexit hook would let exporter errors escape at interpreter exit;
        # we register a fail-safe one instead.
        self.provider = TracerProvider(shutdown_on_exit=False)
        if exporter is not None:
            processor: SpanProcessor
            if batch:
                self._queue = DropCountingBatchProcessor(
                    exporter,
                    max_queue_size=self.max_queue_size,
                    max_export_batch_size=min(_export_batch_size(), self.max_queue_size),
                    run_span_deadline_s=run_span_deadline_s,
                )
                processor = self._queue
            else:
                processor = SimpleSpanProcessor(exporter)
            self.provider.add_span_processor(processor)
        self.tracer = self.provider.get_tracer("agentflight")

    @property
    def dropped_spans(self) -> int:
        """Spans dropped because the export queue was full, since this recorder started."""
        return self._queue.dropped_total if self._queue is not None else 0

    def open_run(self, span: Span) -> None:
        if self._queue is not None:
            self._queue.open_run(span.get_span_context().trace_id)

    def close_run(self, span: Span) -> None:
        """Record on the run span how many of its spans were dropped (before it ends)."""
        if self._queue is not None:
            dropped = self._queue.close_run(span.get_span_context().trace_id)
            if dropped:
                span.set_attribute(A.RUN_DROPPED_SPANS, dropped)

    def flush(self, timeout_millis: int = 30_000) -> bool:
        return bool(_bounded(self.provider.force_flush, timeout_millis, timeout_millis))

    def shutdown(self, timeout_millis: int = DEFAULT_SHUTDOWN_MILLIS) -> None:
        _bounded(self.provider.shutdown, timeout_millis)


_recorder: Recorder = Recorder()


def _shutdown_at_exit() -> None:
    _recorder.shutdown()


atexit.register(_shutdown_at_exit)


def configure(
    endpoint: str = "http://localhost:8000",
    *,
    exporter: SpanExporter | None = None,
    batch: bool = True,
    timeout: float = 5.0,
    api_key: str | None = None,
    max_attr_chars: int | None = DEFAULT_MAX_ATTR_CHARS,
    redact: Redactor | None = None,
    max_export_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_queue_size: int | None = None,
) -> Recorder:
    """Point the SDK at an AgentFlight server (or a custom exporter) and return the recorder.

    ``api_key`` defaults to the ``AGENTFLIGHT_API_KEY`` environment variable.
    ``max_attr_chars`` caps each recorded payload (``None`` disables the cap); truncation
    is recorded in ``agentflight.truncated``. ``redact(key, value)`` runs on every payload
    before it is serialized; if it raises, the value is replaced with a placeholder.
    ``max_export_attempts`` caps the HTTP requests one export makes while splitting
    batches the server rejects as too large (413). ``max_queue_size`` is how many finished
    spans ``batch=True`` holds for export (default ``AGENTFLIGHT_MAX_QUEUE_SIZE``, else
    OpenTelemetry's ``OTEL_BSP_MAX_QUEUE_SIZE``, else 2048); spans beyond it are dropped.
    If setup fails, recording is disabled (a warning is logged) rather than raising.
    """
    global _recorder
    _recorder.shutdown()
    try:
        key = api_key if api_key is not None else os.environ.get("AGENTFLIGHT_API_KEY")
        _recorder = Recorder(
            exporter
            or AgentFlightExporter(
                endpoint, timeout=timeout, api_key=key, max_attempts=max_export_attempts
            ),
            batch=batch,
            max_attr_chars=max_attr_chars,
            redact=redact,
            max_queue_size=max_queue_size,
            run_span_deadline_s=timeout,
        )
    except Exception:
        logger.warning("agentflight: configure failed; recording disabled", exc_info=True)
        _recorder = Recorder()
    return _recorder


def get_recorder() -> Recorder:
    return _recorder


def flush(timeout_millis: int = 30_000) -> bool:
    """Wait at most ``timeout_millis`` for finished spans to be exported. Never raises.

    Returns False if the wait ran out (export goes on in the background) or failed.
    """
    return _recorder.flush(timeout_millis)


def shutdown(timeout_millis: int = DEFAULT_SHUTDOWN_MILLIS) -> None:
    """Export what is queued and stop, waiting at most ``timeout_millis``. Never raises."""
    _recorder.shutdown(timeout_millis)


@contextmanager
def _span(
    name: str,
    attrs: Callable[[], dict[str, Any]],
    *,
    new_trace: bool = False,
) -> Iterator[Span]:
    """Open a recording span that is fail-safe in both directions.

    * If the recorder breaks while opening/closing the span, the agent code still runs and
      a non-recording span is yielded.
    * If the agent code raises, that exact exception propagates; recording it is best effort.
    """
    cm: Any = None
    span: Span = trace.INVALID_SPAN
    try:
        context = trace.set_span_in_context(trace.INVALID_SPAN) if new_trace else None
        cm = _recorder.tracer.start_as_current_span(
            name,
            context=context,
            attributes=_finalize(attrs()),
            record_exception=False,
            set_status_on_exception=False,
        )
        span = cm.__enter__()
    except Exception:
        logger.warning("agentflight: could not start span %r (ignored)", name, exc_info=True)
        cm = None
        span = trace.INVALID_SPAN
    try:
        yield span
    except BaseException as exc:
        _safe(_record_error, span, exc)
        if cm is not None:
            _safe(cm.__exit__, type(exc), exc, exc.__traceback__)
        raise
    else:
        _safe(span.set_status, Status(StatusCode.OK))
        if cm is not None:
            _safe(cm.__exit__, None, None, None)


def _instant(name: str, attrs: Callable[[], dict[str, Any]], exc: BaseException | None) -> None:
    span = _recorder.tracer.start_span(name, attributes=_finalize(attrs()))
    try:
        if exc is None:
            span.set_status(Status(StatusCode.OK))
        else:
            _record_error(span, exc)
    finally:
        span.end()


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

    def attrs() -> dict[str, Any]:
        out: dict[str, Any] = {
            A.KIND: A.RUN,
            A.RUN_NAME: name,
            A.RUN_METADATA: _Payload(metadata or {}),
        }
        session = _replay_session.get()
        if session is not None:
            out.update(session.run_attributes())
        return out

    with _span(f"run:{name}", attrs, new_trace=True) as span:
        recorder = _recorder
        _safe(recorder.open_run, span)
        session = _replay_session.get()
        if session is not None:
            _safe(session.bind_run, span)
        try:
            yield RunHandle(span=span, name=name)
        finally:
            if session is not None:
                _safe(session.unbind_run, span)
            # Before the run span ends, so the count travels with it.
            _safe(recorder.close_run, span)


@dataclass
class LLMCall:
    """Handle yielded by :func:`llm_call`; fill in the response once you have it."""

    span: Span
    model: str
    response: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    # True when a replay served this call from a recording; ``response`` holds the output.
    replayed: bool = False
    _start: float = field(default_factory=time.perf_counter)

    def record(
        self,
        response: Any,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Attach the model's response and usage. Never raises."""
        self.response = response
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd
        _safe(self._set_attributes)

    def _set_attributes(self) -> None:
        _set_payload(self.span, A.LLM_RESPONSE, self.response)
        if self.tokens_in is not None:
            self.span.set_attribute(A.LLM_TOKENS_IN, int(self.tokens_in))
        if self.tokens_out is not None:
            self.span.set_attribute(A.LLM_TOKENS_OUT, int(self.tokens_out))
        if self.cost_usd is not None:
            self.span.set_attribute(A.LLM_COST_USD, float(self.cost_usd))


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

    def attrs() -> dict[str, Any]:
        out: dict[str, Any] = {
            A.KIND: A.LLM,
            A.LLM_MODEL: model,
            A.LLM_MESSAGES: _Payload(messages),
        }
        if params:
            out[A.LLM_PARAMS] = _Payload(params)
        return out

    with _span(name or f"llm:{model}", attrs) as span:
        call = LLMCall(span=span, model=model)
        session = _replay_session.get()
        if session is not None:
            inputs = {A.LLM_MESSAGES: messages, A.LLM_PARAMS: params or None}
            hit, value = session.next(A.LLM, model, inputs, span)
            if hit:
                call.replayed = True
                call.response = value
                _safe(_set_payload, span, A.LLM_RESPONSE, value)
        yield call


def _bind_args(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception:
        return {"args": list(args), "kwargs": kwargs}


def _tool_span(tool_name: str, fn: Callable[..., Any], args: Any, kwargs: Any) -> Any:
    return _span(
        f"tool:{tool_name}",
        lambda: {
            A.KIND: A.TOOL,
            A.TOOL_NAME: tool_name,
            A.TOOL_ARGS: _Payload(_bind_args(fn, args, kwargs)),
        },
    )


def _set_result(span: Span, result: Any) -> None:
    _safe(_set_payload, span, A.TOOL_RESULT, result)


def _replayed_result(
    tool_name: str, fn: Callable[..., Any], args: Any, kwargs: Any, span: Span
) -> tuple[bool, Any]:
    """(True, recorded result) if an active replay serves this tool call."""
    session = _replay_session.get()
    if session is None:
        return False, None
    hit, value = session.next(A.TOOL, tool_name, {A.TOOL_ARGS: _bind_args(fn, args, kwargs)}, span)
    if hit:
        _set_result(span, value)
    return hit, value


@overload
def tool(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def tool(*, name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(
    func: Callable[P, R] | None = None, /, *, name: str | None = None
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator recording each call of a tool: name, arguments, result or error.

    Works on sync and async functions: ``@agentflight.tool`` or ``@agentflight.tool(name=...)``.
    The wrapped function's return value and exceptions are passed through unchanged.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        tool_name: str = name or str(getattr(fn, "__name__", "tool"))

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with _tool_span(tool_name, fn, args, kwargs) as span:
                    hit, value = _replayed_result(tool_name, fn, args, kwargs, span)
                    if hit:
                        return value
                    result = await fn(*args, **kwargs)
                    _set_result(span, result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with _tool_span(tool_name, fn, args, kwargs) as span:
                hit, value = _replayed_result(tool_name, fn, args, kwargs, span)
                if hit:
                    return value  # type: ignore[no-any-return]
                result = fn(*args, **kwargs)
                _set_result(span, result)
                return result

        return wrapper

    if func is not None:
        return decorate(func)
    return decorate


def decision(name: str, choice: Any, *, reason: str | None = None) -> None:
    """Record a decision point (e.g. which tool the agent chose and why). Never raises."""

    def attrs() -> dict[str, Any]:
        out: dict[str, Any] = {
            A.KIND: A.DECISION,
            A.DECISION_NAME: name,
            A.DECISION_CHOICE: _Payload(choice),
        }
        if reason is not None:
            out[A.DECISION_REASON] = str(reason)
        return out

    _safe(_instant, f"decision:{name}", attrs, None)


def state_change(key: str, new: Any, *, old: Any = None) -> None:
    """Record a change to agent state (memory, plan, scratchpad...). Never raises."""
    _safe(
        _instant,
        f"state:{key}",
        lambda: {
            A.KIND: A.STATE,
            A.STATE_KEY: key,
            A.STATE_OLD: _Payload(old),
            A.STATE_NEW: _Payload(new),
        },
        None,
    )


def error(exc: BaseException, *, name: str | None = None) -> None:
    """Record a handled error that did not propagate out of the run. Never raises."""
    _safe(_instant, name or f"error:{type(exc).__name__}", lambda: {A.KIND: A.ERROR}, exc)
