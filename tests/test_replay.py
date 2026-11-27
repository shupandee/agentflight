from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import agentflight
from agentflight import attributes as A
from agentflight.integrations.openai import instrument_openai
from agentflight.replayer import Recording, ReplayDivergence, ReplayedError, ReplayError
from tests.conftest import TEST_KEY

LIVE: Counter[str] = Counter()
WEATHER = {"delhi": "31C, sunny", "paris": "18C, cloudy"}


class StationDown(Exception):
    pass


@agentflight.tool
def get_weather(city: str) -> str:
    LIVE["tool"] += 1
    if city == "Oslo":
        raise StationDown("Oslo station offline")
    try:
        return WEATHER[city.lower()]
    except KeyError:
        raise LookupError(f"no weather data for {city!r}") from None


def fake_model(messages: list[dict[str, str]]) -> str:
    LIVE["llm"] += 1
    last = messages[-1]["content"]
    if last.startswith(("RESULT", "ERROR")):
        return f"Final: {last}"
    return "CALL " + last.split()[-1].strip("?")


def chat(messages: list[dict[str, str]]) -> str:
    with agentflight.llm_call("fake-1", messages) as call:
        if call.replayed:
            return str(call.response)
        reply = fake_model(messages)
        call.record(reply, tokens_in=len(messages), tokens_out=1)
        return reply


def agent(question: str) -> tuple[str, str]:
    messages = [{"role": "user", "content": question}]
    with agentflight.run("weather", metadata={"q": question}) as run:
        for _ in range(3):
            reply = chat(messages)
            messages.append({"role": "assistant", "content": reply})
            if reply.startswith("Final"):
                return run.run_id, reply
            try:
                observation = "RESULT " + get_weather(reply.removeprefix("CALL "))
            except (LookupError, StationDown) as exc:
                agentflight.error(exc)
                observation = f"ERROR {type(exc).__name__}: {exc}"
            messages.append({"role": "user", "content": observation})
    raise RuntimeError("did not finish")


def fetch(client: TestClient, run_id: str) -> Recording:
    return Recording.fetch(run_id, "http://testserver", api_key=TEST_KEY, client=client)


def record(client: TestClient, question: str) -> tuple[Recording, str]:
    run_id, answer = agent(question)
    agentflight.flush()
    LIVE.clear()
    return fetch(client, run_id), answer


def stored(client: TestClient, run_id: str) -> dict[str, Any]:
    agentflight.flush()
    return dict(client.get(f"/v1/runs/{run_id}").json())


def test_replay_serves_recorded_outputs_without_live_calls(recorded: TestClient) -> None:
    recording, answer = record(recorded, "Weather in Delhi?")
    assert [(s.index, s.kind, s.name) for s in recording.steps] == [
        (0, "llm", "fake-1"),
        (1, "tool", "get_weather"),
        (2, "llm", "fake-1"),
    ]

    with agentflight.replay(recording) as session:
        run_id, replayed = agent("Weather in Delhi?")

    assert replayed == answer == "Final: RESULT 31C, sunny"
    assert not LIVE
    assert session.remaining == 0

    run = stored(recorded, run_id)
    assert run_id != recording.run_id
    root = next(e for e in run["events"] if e["kind"] == "run")
    assert root["attributes"][A.REPLAY_SOURCE_RUN] == recording.run_id
    assert root["attributes"][A.REPLAY_MODE] == "replay"
    steps = [e for e in run["events"] if e["kind"] in ("llm", "tool")]
    assert [e["attributes"][A.REPLAY_STEP] for e in steps] == [0, 1, 2]
    assert {e["attributes"][A.REPLAY_SOURCE] for e in steps} == {"cached"}
    # Replayed calls didn't spend tokens; the response is still recorded.
    assert run["tokens_in"] == 0
    assert steps[0]["attributes"][A.LLM_RESPONSE] == "CALL Delhi"


def test_replay_reraises_recorded_builtin_errors(recorded: TestClient) -> None:
    recording, answer = record(recorded, "Weather in Atlantis?")
    assert recording.steps[1].error == ("LookupError", "no weather data for 'Atlantis'")
    with agentflight.replay(recording):
        assert agent("Weather in Atlantis?")[1] == answer
    assert not LIVE


def test_custom_errors_replay_as_replayed_error(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Oslo?")

    @agentflight.tool(name="get_weather")
    def strict_weather(city: str) -> str:
        return get_weather(city)

    with agentflight.replay(recording), pytest.raises(ReplayedError) as info:
        chat([{"role": "user", "content": "Weather in Oslo?"}])
        strict_weather("Oslo")
    assert (info.value.error_type, info.value.message) == ("StationDown", "Oslo station offline")
    assert not LIVE


def test_changed_input_diverges(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.replay(recording), pytest.raises(ReplayDivergence, match="step 0"):
        agent("Weather in Paris?")
    assert not LIVE


def test_changed_tool_args_and_names_diverge(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    messages = [{"role": "user", "content": "Weather in Delhi?"}]
    with agentflight.replay(recording):
        chat(messages)
        with pytest.raises(ReplayDivergence, match=r"agentflight\.tool\.args differs"):
            get_weather("Paris")
    with (
        agentflight.replay(recording),
        pytest.raises(ReplayDivergence, match="recorded llm 'fake-1', got tool"),
    ):
        get_weather("Delhi")
    assert not LIVE


def test_extra_calls_diverge_and_fewer_calls_leave_steps(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    messages = [{"role": "user", "content": "Weather in Delhi?"}]
    with agentflight.replay(recording) as session:
        chat(messages)
        assert session.remaining == 2
    with agentflight.replay(recording):
        agent("Weather in Delhi?")
        with pytest.raises(ReplayDivergence, match="not in the recording"):
            get_weather("Delhi")
    assert not LIVE


def test_async_tools_replay(recorded: TestClient) -> None:
    @agentflight.tool
    async def fetch_page(url: str) -> dict[str, Any]:
        LIVE["tool"] += 1
        return {"url": url, "status": 200}

    async def main() -> tuple[str, Any]:
        with agentflight.run("async") as r:
            return r.run_id, await fetch_page("https://example.invalid")

    run_id, result = asyncio.run(main())
    agentflight.flush()
    recording = fetch(recorded, run_id)
    LIVE.clear()
    with agentflight.replay(recording):
        assert asyncio.run(main())[1] == result
    assert not LIVE


def test_nested_calls_are_part_of_their_step(recorded: TestClient) -> None:
    @agentflight.tool
    def research(topic: str) -> str:
        return chat([{"role": "user", "content": f"about {topic}"}])

    with agentflight.run("nested") as r:
        research("x")
    recording = fetch(recorded, r.run_id)
    assert [(s.kind, s.name) for s in recording.steps] == [("tool", "research")]
    LIVE.clear()
    with agentflight.replay(recording):
        assert research("x") == "CALL x"
    assert not LIVE


def test_truncated_inputs_match_but_truncated_outputs_refuse(recorded: TestClient) -> None:
    exporter = agentflight.AgentFlightExporter("http://testserver", client=recorded)
    agentflight.configure(exporter=exporter, batch=False, max_attr_chars=60)

    @agentflight.tool
    def report(city: str) -> str:
        LIVE["tool"] += 1
        return "sunny " * 40

    question = "Please tell me, in as much detail as you can, the weather in Delhi?"
    with agentflight.run("long") as r:
        chat([{"role": "user", "content": question}])
        report("Delhi")
    recording = fetch(recorded, r.run_id)
    assert A.LLM_MESSAGES in recording.steps[0].truncated
    LIVE.clear()

    with agentflight.replay(recording):
        assert chat([{"role": "user", "content": question}]) == "CALL Delhi"
        with pytest.raises(
            ReplayError, match=r"step 1: recorded agentflight\.tool\.result is trunc"
        ):
            report("Delhi")
    assert not LIVE


def test_save_and_load_round_trip(recorded: TestClient, tmp_path: Path) -> None:
    recording, answer = record(recorded, "Weather in Delhi?")
    path = tmp_path / "run.json"
    recording.save(path)
    loaded = Recording.load(path)
    assert loaded.steps == recording.steps
    with agentflight.replay(loaded):
        assert agent("Weather in Delhi?")[1] == answer
    assert json.loads(path.read_text())["id"] == recording.run_id


def test_fetch_errors_are_replay_errors(recorded: TestClient) -> None:
    with pytest.raises(ReplayError, match="HTTP 404"):
        fetch(recorded, "f" * 32)
    with pytest.raises(ReplayError, match="HTTP 401"):
        Recording.fetch("f" * 32, "http://testserver", api_key="wrong", client=recorded)
    with pytest.raises(ReplayError, match="not a run"):
        Recording.from_run({"events": []})


def test_instrumented_openai_client_is_not_called_during_replay(recorded: TestClient) -> None:
    openai = pytest.importorskip("openai")
    import httpx

    from tests.test_examples import load

    example = load("openai_agent")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return example._scripted(request)

    client = instrument_openai(
        openai.OpenAI(
            api_key="offline",
            base_url="http://offline.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    answer = example.agent(client, "What is the weather in Delhi?")
    assert len(requests) == 2
    agentflight.flush()
    (run,) = recorded.get("/v1/runs").json()["runs"]
    recording = fetch(recorded, run["id"])
    assert [s.kind for s in recording.steps] == ["llm", "tool", "llm"]

    requests.clear()
    with agentflight.replay(recording) as session:
        assert example.agent(client, "What is the weather in Delhi?") == answer
    assert requests == []
    assert session.remaining == 0


def test_replay_is_scoped_to_its_block(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.replay(recording):
        pass
    with agentflight.llm_call("fake-1", []) as call:
        assert not call.replayed
    assert get_weather("Delhi") == "31C, sunny"
    assert LIVE["tool"] == 1


def run_event(client: TestClient, run_id: str) -> dict[str, Any]:
    return next(e for e in stored(client, run_id)["events"] if e["kind"] == "run")


def test_on_divergence_live_runs_live_from_the_first_mismatch(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.replay(recording, on_divergence="live") as session:
        run_id, answer = agent("Weather in Paris?")

    assert answer == "Final: RESULT 18C, cloudy"
    assert (LIVE["llm"], LIVE["tool"]) == (2, 1)
    assert session.diverged_at == 0
    assert session.divergence is not None and "step 0" in session.divergence
    root = run_event(recorded, run_id)
    assert root["attributes"][A.REPLAY_DIVERGED_AT] == 0
    assert root["attributes"][A.REPLAY_DIVERGENCE] == session.divergence
    steps = [e for e in stored(recorded, run_id)["events"] if e["kind"] in ("llm", "tool")]
    assert {e["attributes"][A.REPLAY_SOURCE] for e in steps} == {"live"}


def test_on_divergence_live_keeps_the_matching_prefix_cached(recorded: TestClient) -> None:
    recording, answer = record(recorded, "Weather in Delhi?")
    with (
        agentflight.replay(recording, on_divergence="live") as session,
        agentflight.run("extra") as r,
    ):
        assert agent("Weather in Delhi?")[1] == answer
        assert not LIVE
        assert get_weather("Paris") == "18C, cloudy"  # beyond the recording
        assert get_weather("Delhi") == "31C, sunny"  # stays live after diverging
    assert LIVE["tool"] == 2
    assert session.diverged_at == 3
    assert "not in the recording" in (session.divergence or "")
    # The divergence is noted on the innermost run open when it happened.
    assert run_event(recorded, r.run_id)["attributes"][A.REPLAY_DIVERGED_AT] == 3


def test_on_divergence_is_validated(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with (
        pytest.raises(ValueError, match="on_divergence"),
        agentflight.replay(
            recording,
            on_divergence="skip",  # type: ignore[arg-type]
        ),
    ):
        pass
