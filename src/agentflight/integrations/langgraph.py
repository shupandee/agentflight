"""LangGraph instrumentation through LangChain callbacks.

Needs ``langchain-core`` (installed with ``langgraph``; ``pip install agentflight[langgraph]``).
Pass the handler in the run config; nothing in your graph changes::

    from agentflight.integrations.langgraph import AgentFlightCallbackHandler

    graph.invoke(inputs, config={"callbacks": [AgentFlightCallbackHandler()]})

Recorded:

* the graph as a ``run`` (or, inside an ``agentflight.run``, as a ``node`` span in that run),
* each executed graph node as a ``node`` span with its step, input state and state update,
* chat model / LLM calls as ``llm`` spans with messages and token usage,
* tool calls as ``tool`` spans with arguments and result,
* errors on the span where they happened (one failure id per exception, as in the SDK).

LangChain's internal runnables inside a node (prompts, sequences, routers) aren't recorded;
their children are attached to the nearest recorded ancestor.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from agentflight import attributes as A
from agentflight import sdk

logger = logging.getLogger("agentflight")

INTEGRATION = "langgraph"

# Control-flow exceptions LangGraph raises for interrupts and parent commands.
_BUBBLE_UP = "GraphBubbleUp"

_SECRET_PARTS = ("api_key", "secret", "password", "authorization")


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Turn LangChain messages and pydantic models into plain JSON-ish values."""
    if depth > 20:
        return value
    if isinstance(value, BaseMessage):
        out: dict[str, Any] = {"type": value.type, "content": _jsonable(value.content, depth + 1)}
        for name in ("name", "tool_calls", "tool_call_id"):
            item = getattr(value, name, None)
            if item:
                out[name] = _jsonable(item, depth + 1)
        return out
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v, depth + 1) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump) and not isinstance(value, type):
        return dump(mode="json")
    return value


def _params(invocation_params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        k: v
        for k, v in (invocation_params or {}).items()
        if not any(part in k.lower() for part in _SECRET_PARTS)
    }


def _model_name(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    params = kwargs.get("invocation_params") or {}
    metadata = kwargs.get("metadata") or {}
    for value in (
        params.get("model"),
        params.get("model_name"),
        metadata.get("ls_model_name"),
        kwargs.get("name"),
        (serialized or {}).get("name"),
    ):
        if value:
            return str(value)
    return "unknown"


def _is_bubble_up(exc: BaseException) -> bool:
    return any(cls.__name__ == _BUBBLE_UP for cls in type(exc).__mro__)


def _usage(response: Any) -> tuple[Any, int | None, int | None]:
    """(recorded response, tokens_in, tokens_out) from a LangChain ``LLMResult``."""
    generations = [g for batch in response.generations for g in batch]
    first = generations[0] if generations else None
    message = getattr(first, "message", None)
    recorded: Any = _jsonable(message) if message is not None else getattr(first, "text", None)
    usage = getattr(message, "usage_metadata", None) or {}
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if tokens_in is None and tokens_out is None:
        token_usage = (response.llm_output or {}).get("token_usage") or {}
        tokens_in = token_usage.get("prompt_tokens")
        tokens_out = token_usage.get("completion_tokens")
    return recorded, tokens_in, tokens_out


class AgentFlightCallbackHandler(BaseCallbackHandler):
    """Records LangGraph runs as AgentFlight spans. Never raises into the graph.

    ``run_name`` and ``metadata`` are used when the handler creates the run itself (no
    surrounding ``agentflight.run``). ``run_id`` holds the id of the last run it created.
    """

    raise_error = False
    run_inline = True

    def __init__(self, *, run_name: str | None = None, metadata: dict[str, Any] | None = None):
        super().__init__()
        self.run_name = run_name
        self.metadata = metadata or {}
        self.run_id: str | None = None
        self._spans: dict[UUID, Span] = {}
        # Unrecorded LangChain runs -> nearest recorded ancestor (or None).
        self._skipped: dict[UUID, UUID | None] = {}
        self._lock = threading.Lock()

    # -- span bookkeeping -------------------------------------------------------------

    def _guard(self, fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        sdk._safe(fn, *args, **kwargs)

    def _resolve(self, parent_run_id: UUID | None) -> UUID | None:
        seen = 0
        while parent_run_id is not None and parent_run_id not in self._spans and seen < 1000:
            parent_run_id = self._skipped.get(parent_run_id)
            seen += 1
        return parent_run_id if parent_run_id in self._spans else None

    def _open(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        name: str,
        attrs: dict[str, Any],
        *,
        new_trace: bool = False,
    ) -> None:
        tracer = sdk.get_recorder().tracer
        with self._lock:
            parent = self._resolve(parent_run_id)
            if parent is not None:
                context = trace.set_span_in_context(self._spans[parent])
            elif new_trace:
                context = trace.set_span_in_context(trace.INVALID_SPAN)
            else:
                context = None  # the current span, e.g. a surrounding agentflight.run
            attrs = {A.INTEGRATION: INTEGRATION, **attrs}
            self._spans[run_id] = tracer.start_span(
                name, context=context, attributes=sdk._finalize(attrs)
            )

    def _skip(self, run_id: UUID, parent_run_id: UUID | None) -> None:
        with self._lock:
            self._skipped[run_id] = self._resolve(parent_run_id)

    def _close(
        self,
        run_id: UUID,
        payloads: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            self._skipped.pop(run_id, None)
            span = self._spans.pop(run_id, None)
        if span is None:
            return
        try:
            for key, value in (payloads or {}).items():
                sdk._safe(sdk._set_payload, span, key, value)
            if error is None:
                span.set_status(Status(StatusCode.OK))
            elif _is_bubble_up(error):
                sdk._safe(
                    sdk._set_payload, span, A.NODE_OUTPUT, {"interrupted": type(error).__name__}
                )
                span.set_status(Status(StatusCode.OK))
            else:
                sdk._record_error(span, error)
        finally:
            span.end()

    # -- chains: the graph and its nodes ----------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._guard(
            self._chain_start, serialized, inputs, run_id, parent_run_id, tags, metadata, kwargs
        )

    def _chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        run_id: UUID,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
        kwargs: dict[str, Any],
    ) -> None:
        name = str(kwargs.get("name") or (serialized or {}).get("name") or "graph")
        metadata = metadata or {}
        if parent_run_id is None:
            current = trace.get_current_span()
            inside_run = A.KIND in (getattr(current, "attributes", None) or {})
            if inside_run:
                attrs: dict[str, Any] = {A.KIND: A.NODE, A.NODE_NAME: name}
                attrs[A.NODE_INPUT] = sdk._Payload(_jsonable(inputs))
                self._open(run_id, None, f"graph:{name}", attrs)
                return
            run_name = self.run_name or name
            attrs = {
                A.KIND: A.RUN,
                A.RUN_NAME: run_name,
                A.RUN_METADATA: sdk._Payload(self.metadata),
                A.NODE_INPUT: sdk._Payload(_jsonable(inputs)),
            }
            self._open(run_id, None, f"run:{run_name}", attrs, new_trace=True)
            span = self._spans.get(run_id)
            if span is not None:
                self.run_id = format(span.get_span_context().trace_id, "032x")
            return
        is_node = name == metadata.get("langgraph_node") and any(
            t.startswith("graph:step:") for t in tags or []
        )
        if not is_node:
            self._skip(run_id, parent_run_id)
            return
        attrs = {
            A.KIND: A.NODE,
            A.NODE_NAME: name,
            A.NODE_INPUT: sdk._Payload(_jsonable(inputs)),
        }
        step = metadata.get("langgraph_step")
        if isinstance(step, int):
            attrs[A.NODE_STEP] = step
        self._open(run_id, parent_run_id, f"node:{name}", attrs)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._guard(lambda: self._close(run_id, {A.NODE_OUTPUT: _jsonable(outputs)}))

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._guard(self._close, run_id, None, error)

    # -- model calls ------------------------------------------------------------------

    def _llm_start(
        self,
        serialized: dict[str, Any] | None,
        messages: Any,
        run_id: UUID,
        parent_run_id: UUID | None,
        kwargs: dict[str, Any],
    ) -> None:
        model = _model_name(serialized, kwargs)
        attrs: dict[str, Any] = {
            A.KIND: A.LLM,
            A.LLM_MODEL: model,
            A.LLM_MESSAGES: sdk._Payload(_jsonable(messages)),
        }
        params = _params(kwargs.get("invocation_params"))
        if params:
            attrs[A.LLM_PARAMS] = sdk._Payload(params)
        self._open(run_id, parent_run_id, f"llm:{model}", attrs)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        batch: Any = messages[0] if len(messages) == 1 else messages
        self._guard(self._llm_start, serialized, batch, run_id, parent_run_id, kwargs)

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        batch: Any = prompts[0] if len(prompts) == 1 else prompts
        self._guard(self._llm_start, serialized, batch, run_id, parent_run_id, kwargs)

    def _llm_end(self, response: Any, run_id: UUID) -> None:
        with self._lock:
            span = self._spans.get(run_id)
        if span is not None:
            recorded, tokens_in, tokens_out = _usage(response)
            if tokens_in is not None:
                span.set_attribute(A.LLM_TOKENS_IN, int(tokens_in))
            if tokens_out is not None:
                span.set_attribute(A.LLM_TOKENS_OUT, int(tokens_out))
            self._close(run_id, {A.LLM_RESPONSE: recorded})

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        def end() -> None:
            try:
                self._llm_end(response, run_id)
            finally:
                self._close(run_id)  # no-op unless _llm_end failed part way

        self._guard(end)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._guard(self._close, run_id, None, error)

    # -- tools ------------------------------------------------------------------------

    def _tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        run_id: UUID,
        parent_run_id: UUID | None,
        kwargs: dict[str, Any],
    ) -> None:
        name = str((serialized or {}).get("name") or kwargs.get("name") or "tool")
        inputs = kwargs.get("inputs")
        attrs = {
            A.KIND: A.TOOL,
            A.TOOL_NAME: name,
            A.TOOL_ARGS: sdk._Payload(_jsonable(inputs) if inputs is not None else input_str),
        }
        self._open(run_id, parent_run_id, f"tool:{name}", attrs)

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._guard(self._tool_start, serialized, input_str, run_id, parent_run_id, kwargs)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._guard(lambda: self._close(run_id, {A.TOOL_RESULT: _jsonable(output)}))

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._guard(self._close, run_id, None, error)
