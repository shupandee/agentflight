"""Record a LangGraph ReAct agent with the AgentFlight callback handler.

The chat model is LangChain's scripted fake so the example runs offline; swap in any
LangChain chat model (e.g. ChatOpenAI) and the same handler records real calls and usage.

    pip install -e ".[langgraph]"
    uvicorn agentflight.server.main:app        # in one terminal
    python examples/langgraph_agent.py         # in another
"""

from __future__ import annotations

import os
import warnings
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import agentflight
from agentflight.integrations.langgraph import AgentFlightCallbackHandler

with warnings.catch_warnings():
    # LangGraph 1.x points to langchain.agents; the prebuilt still works without langchain.
    warnings.simplefilter("ignore", DeprecationWarning)
    from langgraph.prebuilt import create_react_agent

ENDPOINT = os.environ.get("AGENTFLIGHT_ENDPOINT", "http://localhost:8000")


@tool
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return {"paris": "18C, cloudy", "delhi": "31C, sunny"}.get(city.lower(), "unknown")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def build_agent() -> Any:
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "get_weather", "args": {"city": "Delhi"}, "id": "t1"}],
            ),
            AIMessage(content="It is 31C and sunny in Delhi."),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return create_react_agent(model, [get_weather])


def main() -> str:
    handler = AgentFlightCallbackHandler(run_name="langgraph-weather-agent")
    out = build_agent().invoke(
        {"messages": [("user", "What is the weather in Delhi?")]},
        config={"callbacks": [handler]},
    )
    print(f"run_id={handler.run_id}")
    return str(out["messages"][-1].content)


if __name__ == "__main__":
    agentflight.configure(ENDPOINT)
    print(main())
    agentflight.flush()
    agentflight.shutdown()
