"""Record a tool-calling agent built on an OpenAI-style chat client.

With OPENAI_API_KEY set this calls the real API (OPENAI_BASE_URL points it at any
OpenAI-compatible server, e.g. a local model). Without a key it uses a scripted fake
transport, so the example runs offline and costs nothing.

    pip install -e ".[openai]"
    uvicorn agentflight.server.main:app        # in one terminal
    python examples/openai_agent.py            # in another
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from openai import OpenAI

import agentflight
from agentflight.integrations.openai import instrument_openai

ENDPOINT = os.environ.get("AGENTFLIGHT_ENDPOINT", "http://localhost:8000")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

WEATHER = {"paris": "18C, cloudy", "delhi": "31C, sunny"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


@agentflight.tool
def get_weather(city: str) -> str:
    try:
        return WEATHER[city.lower()]
    except KeyError:
        raise LookupError(f"no weather data for {city!r}") from None


def _scripted(request: httpx.Request) -> httpx.Response:
    """Offline stand-in for the API: asks for the tool once, then answers."""
    messages = json.loads(request.content)["messages"]
    last = messages[-1]
    if last["role"] == "tool":
        message: dict[str, Any] = {"role": "assistant", "content": f"Final: {last['content']}"}
        finish = "stop"
    else:
        city = last["content"].rstrip("?").split()[-1]
        call = {"name": "get_weather", "arguments": json.dumps({"city": city})}
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"call_{len(messages)}", "type": "function", "function": call}],
        }
        finish = "tool_calls"
    words = sum(len(str(m.get("content") or "").split()) for m in messages)
    return httpx.Response(
        200,
        json={
            "id": f"fake-{len(messages)}",
            "object": "chat.completion",
            "created": 0,
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            # Word counts stand in for token usage in offline mode.
            "usage": {"prompt_tokens": words, "completion_tokens": 5, "total_tokens": words + 5},
        },
    )


def make_client() -> OpenAI:
    if os.environ.get("OPENAI_API_KEY"):
        client = OpenAI()
    else:
        client = OpenAI(
            api_key="offline",
            base_url="http://offline.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(_scripted)),
        )
    return instrument_openai(client)


def agent(client: OpenAI, question: str) -> str:
    messages: list[Any] = [{"role": "user", "content": question}]
    with agentflight.run("openai-weather-agent", metadata={"question": question}) as run:
        print(f"run_id={run.run_id}")
        for _ in range(4):
            resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                args = json.loads(call.function.arguments)
                try:
                    content = get_weather(**args)
                except LookupError as exc:
                    agentflight.error(exc)
                    content = f"error: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        raise RuntimeError("agent did not finish in 4 steps")


if __name__ == "__main__":
    agentflight.configure(ENDPOINT)
    client = make_client()
    print(agent(client, "What is the weather in Delhi?"))
    print(agent(client, "What is the weather in Atlantis?"))
    agentflight.flush()
    agentflight.shutdown()
