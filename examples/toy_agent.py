"""Record a toy agent run end to end.

The "model" here is a scripted fake so the example needs no API key; swap in a real
client and pass real token counts/costs from its response.

    uvicorn agentflight.server.main:app        # in one terminal
    python examples/toy_agent.py               # in another
    curl localhost:8000/v1/runs/<run_id>
"""

from __future__ import annotations

import os

import agentflight

ENDPOINT = os.environ.get("AGENTFLIGHT_ENDPOINT", "http://localhost:8000")

# Made-up price for the fake model so the example exercises cost tracking.
TOY_USD_PER_TOKEN = 0.000002

WEATHER = {"paris": "18C, cloudy", "delhi": "31C, sunny"}


@agentflight.tool
def get_weather(city: str) -> str:
    try:
        return WEATHER[city.lower()]
    except KeyError:
        raise LookupError(f"no weather data for {city!r}") from None


def fake_model(messages: list[dict[str, str]]) -> str:
    last = messages[-1]["content"]
    if last.startswith("TOOL_RESULT"):
        return f"Final answer: {last.removeprefix('TOOL_RESULT ')}"
    if last.startswith("TOOL_ERROR"):
        return "Final answer: sorry, I couldn't find that."
    return "CALL get_weather " + last.split()[-1].strip("?")


def chat(messages: list[dict[str, str]]) -> str:
    with agentflight.llm_call("fake-model-1", messages) as call:
        reply = fake_model(messages)
        # A real client would report usage; here we count words as a stand-in.
        tokens_in = sum(len(m["content"].split()) for m in messages)
        tokens_out = len(reply.split())
        cost = (tokens_in + tokens_out) * TOY_USD_PER_TOKEN
        call.record(reply, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
    return reply


def agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    with agentflight.run("weather-agent", metadata={"question": question}) as run:
        print(f"run_id={run.run_id}")
        for step in range(3):
            reply = chat(messages)
            messages.append({"role": "assistant", "content": reply})
            if reply.startswith("Final answer"):
                agentflight.decision("finish", reply, reason=f"answered at step {step}")
                return reply
            city = reply.split()[-1]
            agentflight.decision("call_tool", {"tool": "get_weather", "city": city})
            try:
                observation = "TOOL_RESULT " + get_weather(city)
            except LookupError as exc:
                agentflight.error(exc)
                observation = f"TOOL_ERROR {exc}"
            agentflight.state_change("messages", len(messages) + 1, old=len(messages))
            messages.append({"role": "user", "content": observation})
        raise RuntimeError("agent did not finish in 3 steps")


if __name__ == "__main__":
    agentflight.configure(ENDPOINT)
    print(agent("What is the weather in Delhi?"))
    print(agent("What is the weather in Atlantis?"))
    agentflight.flush()
    agentflight.shutdown()
