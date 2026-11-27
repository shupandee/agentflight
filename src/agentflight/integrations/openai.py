"""Instrumentation for OpenAI-style chat completion clients.

Works with the ``openai`` package (``OpenAI`` and ``AsyncOpenAI``) and any client with the
same ``client.chat.completions.create(model=..., messages=...)`` shape. The ``openai``
package itself is never imported here, so it isn't a dependency::

    from openai import OpenAI
    from agentflight.integrations.openai import instrument_openai

    client = instrument_openai(OpenAI())

Each ``create`` call becomes an ``llm`` span with the model, messages, request params
(tools, temperature, ...), the full response (including tool calls) and token usage from
``response.usage``. ``stream=True`` calls are passed through without recording.

During ``agentflight.replay`` the client isn't called: the recorded response is returned,
rebuilt as an ``openai`` ``ChatCompletion`` when that package is installed.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from agentflight import attributes as A
from agentflight import sdk

logger = logging.getLogger("agentflight")

C = TypeVar("C")

CostFn = Callable[[str, int | None, int | None], float | None]

INTEGRATION = "openai"

# Transport options, not model inputs. Never recorded: extra_headers can carry credentials.
_TRANSPORT_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})

_WRAPPED = "__agentflight_wrapped__"


def _split(kwargs: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
    model = str(kwargs.get("model", "unknown"))
    messages = kwargs.get("messages")
    params = {
        k: v
        for k, v in kwargs.items()
        if k not in ("model", "messages", "stream") and k not in _TRANSPORT_KWARGS
    }
    return model, messages, params


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_json(response: Any) -> Any:
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_unset=True)
    return response


class _AttrDict(dict[str, Any]):
    """A dict that also allows attribute access, standing in for a response object."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(self)


def _attr_view(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({k: _attr_view(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_attr_view(v) for v in value]
    return value


def _rebuild(recorded: Any) -> Any:
    """Turn a recorded response back into a ``ChatCompletion`` (or an attribute view)."""
    try:
        from openai.types.chat import ChatCompletion

        return ChatCompletion.model_validate(recorded)
    except Exception:
        return _attr_view(recorded)


def _record(call: sdk.LLMCall, model: str, response: Any, cost: CostFn | None) -> None:
    usage = _get(response, "usage")
    tokens_in = _get(usage, "prompt_tokens") if usage is not None else None
    tokens_out = _get(usage, "completion_tokens") if usage is not None else None
    cost_usd = None
    if cost is not None:
        try:
            cost_usd = cost(model, tokens_in, tokens_out)
        except Exception:
            logger.warning("agentflight: cost function failed (ignored)", exc_info=True)
    call.record(_as_json(response), tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)


def _wrap(create: Callable[..., Any], cost: CostFn | None) -> Callable[..., Any]:
    def prepare(kwargs: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
        split = sdk._safe(_split, kwargs)
        return split if split is not None else ("unknown", None, {})

    def tag(call: sdk.LLMCall) -> None:
        sdk._safe(call.span.set_attribute, A.INTEGRATION, INTEGRATION)

    if inspect.iscoroutinefunction(inspect.unwrap(create)):

        @functools.wraps(create)
        async def acreate(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                return await create(*args, **kwargs)
            model, messages, params = prepare(kwargs)
            with sdk.llm_call(model, messages, params=params) as call:
                tag(call)
                if call.replayed:
                    return _rebuild(call.response)
                response = await create(*args, **kwargs)
                sdk._safe(_record, call, model, response, cost)
                return response

        setattr(acreate, _WRAPPED, True)
        return acreate

    @functools.wraps(create)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return create(*args, **kwargs)
        model, messages, params = prepare(kwargs)
        with sdk.llm_call(model, messages, params=params) as call:
            tag(call)
            if call.replayed:
                return _rebuild(call.response)
            response = create(*args, **kwargs)
            sdk._safe(_record, call, model, response, cost)
            return response

    setattr(wrapper, _WRAPPED, True)
    return wrapper


def instrument_openai(client: C, *, cost: CostFn | None = None) -> C:
    """Record every ``client.chat.completions.create`` call. Returns the same client.

    ``cost(model, tokens_in, tokens_out)`` may return a USD cost for the call; AgentFlight
    has no built-in price table. Instrumenting twice is a no-op. If the client doesn't have
    the expected shape, a warning is logged and the client is returned unchanged.
    """
    try:
        completions = client.chat.completions  # type: ignore[attr-defined]
        create = completions.create
        if not getattr(create, _WRAPPED, False):
            completions.create = _wrap(create, cost)
    except Exception:
        logger.warning(
            "agentflight: could not instrument %r (ignored)", type(client), exc_info=True
        )
    return client
