"""Record the toy agent, replay it from the recording, then fork it at the tool step.

uvicorn agentflight.server.main:app        # in one terminal
python examples/replay_agent.py            # in another
"""

from __future__ import annotations

import os

import httpx
import toy_agent

import agentflight

ENDPOINT = os.environ.get("AGENTFLIGHT_ENDPOINT", "http://localhost:8000")
QUESTION = "What is the weather in Delhi?"


def main() -> None:
    agentflight.configure(ENDPOINT)
    print("recorded:", toy_agent.agent(QUESTION))
    agentflight.flush()
    recording = agentflight.Recording.fetch(latest_run_id(), ENDPOINT)
    for step in recording.steps:
        print(f"  step {step.index}: {step.kind} {step.name}")

    # Same code, no model or tool calls: every output comes from the recording.
    with agentflight.replay(recording):
        print("replayed:", toy_agent.agent(QUESTION))

    # Steps before the tool call replay, the tool returns a new value, the rest runs live.
    tool_step = next(s.index for s in recording.steps if s.kind == "tool")
    with agentflight.fork(recording, at=tool_step, output="-5C, snow"):
        print("forked:  ", toy_agent.agent(QUESTION))

    agentflight.flush()
    agentflight.shutdown()


def latest_run_id() -> str:
    key = os.environ.get("AGENTFLIGHT_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = httpx.get(f"{ENDPOINT}/v1/runs", params={"limit": 1}, headers=headers)
    resp.raise_for_status()
    return str(resp.json()["runs"][0]["id"])


if __name__ == "__main__":
    main()
