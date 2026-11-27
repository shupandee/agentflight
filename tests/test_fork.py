from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import agentflight
from agentflight import attributes as A
from agentflight.replayer import ReplayDivergence, ReplayError
from tests.test_replay import LIVE, agent, chat, get_weather, record, stored


def step_sources(client: TestClient, run_id: str) -> list[tuple[int, str]]:
    events = stored(client, run_id)["events"]
    return [
        (e["attributes"][A.REPLAY_STEP], e["attributes"][A.REPLAY_SOURCE])
        for e in events
        if e["kind"] in ("llm", "tool")
    ]


def test_fork_with_substituted_tool_output(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.fork(recording, at=1, output="-5C, snow") as session:
        run_id, answer = agent("Weather in Delhi?")

    assert answer == "Final: RESULT -5C, snow"
    # Step 0 cached, step 1 substituted, step 2 (the model's answer) ran live.
    assert (LIVE["llm"], LIVE["tool"]) == (1, 0)
    assert step_sources(recorded, run_id) == [(0, "cached"), (1, "override"), (2, "live")]
    root = next(e for e in stored(recorded, run_id)["events"] if e["kind"] == "run")
    assert root["attributes"][A.REPLAY_MODE] == "fork"
    assert root["attributes"][A.REPLAY_FORK_STEP] == 1
    assert root["attributes"][A.REPLAY_SOURCE_RUN] == recording.run_id
    assert session.cursor == 3


def test_fork_runs_changed_code_live_from_the_fork_step(
    recorded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    # The tool's data changed since the recording; forking at the tool step picks it up.
    from tests import test_replay

    monkeypatch.setitem(test_replay.WEATHER, "delhi", "29C, haze")
    with agentflight.fork(recording, at=1):
        _, answer = agent("Weather in Delhi?")
    assert answer == "Final: RESULT 29C, haze"
    assert (LIVE["llm"], LIVE["tool"]) == (1, 1)


def test_fork_prefix_must_still_match(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with (
        agentflight.fork(recording, at=2),
        pytest.raises(ReplayDivergence, match=r"step 1 .*tool\.args differs"),
    ):
        assert chat([{"role": "user", "content": "Weather in Delhi?"}]) == "CALL Delhi"
        get_weather("Paris")
    assert not LIVE


def test_override_must_match_the_step_kind(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with (
        agentflight.fork(recording, at=0, output="x"),
        pytest.raises(ReplayDivergence, match="recorded llm 'fake-1', got tool"),
    ):
        get_weather("Delhi")


def test_fork_at_end_replays_everything_then_runs_live(recorded: TestClient) -> None:
    recording, answer = record(recorded, "Weather in Delhi?")
    with agentflight.fork(recording, at=len(recording.steps)):
        assert agent("Weather in Delhi?")[1] == answer
        assert not LIVE
        assert get_weather("Paris") == "18C, cloudy"
    assert LIVE["tool"] == 1


@pytest.mark.parametrize(
    ("at", "kwargs", "match"),
    [(-1, {}, "out of range"), (4, {}, "out of range"), (3, {"output": "x"}, "no step 3")],
)
def test_fork_step_is_validated(
    recorded: TestClient, at: int, kwargs: dict[str, Any], match: str
) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with pytest.raises(ReplayError, match=match), agentflight.fork(recording, at=at, **kwargs):
        pass


def test_fork_on_divergence_live(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.fork(recording, at=2, on_divergence="live") as session:
        assert chat([{"role": "user", "content": "Weather in Delhi?"}]) == "CALL Delhi"
        assert get_weather("Paris") == "18C, cloudy"  # step 1 differs: live from here
    assert (LIVE["llm"], LIVE["tool"]) == (0, 1)
    assert session.diverged_at == 1


def test_fork_override_kind_mismatch_goes_live(recorded: TestClient) -> None:
    recording, _ = record(recorded, "Weather in Delhi?")
    with agentflight.fork(recording, at=0, output="x", on_divergence="live") as session:
        assert get_weather("Delhi") == "31C, sunny"
    assert LIVE["tool"] == 1
    assert session.diverged_at == 0
