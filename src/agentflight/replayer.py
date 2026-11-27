"""Deterministic replay of recorded runs.

A replay re-runs your agent code while serving every recorded model and tool output from
a recording, so no model API or tool is called::

    recording = agentflight.Recording.fetch(run_id)
    with agentflight.replay(recording):
        answer = my_agent(question)  # same code as the recorded run

What is intercepted: ``@agentflight.tool`` functions, clients instrumented with
``instrument_openai``, and ``agentflight.llm_call`` blocks that check ``call.replayed``.
Each of those calls is matched, in order, against the recorded steps. The call must have
the same kind, model or tool name and inputs (messages and params, or tool arguments) as
the recorded step, otherwise :class:`ReplayDivergence` is raised. Replay fails closed: it
never falls back to calling the real service.
"""

from __future__ import annotations

import builtins
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.trace import Span

from agentflight import attributes as A
from agentflight import sdk

STEP_KINDS = (A.LLM, A.TOOL)
_OUTPUT_KEY = {A.LLM: A.LLM_RESPONSE, A.TOOL: A.TOOL_RESULT}


class ReplayError(Exception):
    """A recording can't be loaded or a step can't be replayed."""


class ReplayDivergence(ReplayError):
    """The live run made a call that doesn't match the recording at that step."""


class ReplayedError(Exception):
    """A recorded step failed with an exception type that can't be rebuilt.

    Built-in exception types (``LookupError``, ``ValueError``...) are re-raised as that
    type; anything else is raised as this, with the original type name and message.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class Step:
    """One replayable model or tool call from a recorded run."""

    index: int
    kind: str  # "llm" or "tool"
    name: str  # model name or tool name
    span_id: str
    inputs: dict[str, Any]  # attribute key -> recorded value
    output: Any
    error: tuple[str, str] | None  # (type name, message) if the call failed
    truncated: dict[str, Any] = field(default_factory=dict)


def _steps(events: list[dict[str, Any]]) -> list[Step]:
    by_id = {e["span_id"]: e for e in events}

    def inside_step(event: dict[str, Any]) -> bool:
        parent, seen = event.get("parent_span_id"), 0
        while parent in by_id and seen < 10_000:
            if by_id[parent]["kind"] in STEP_KINDS:
                return True
            parent, seen = by_id[parent].get("parent_span_id"), seen + 1
        return False

    candidates = [e for e in events if e.get("kind") in STEP_KINDS and not inside_step(e)]
    candidates.sort(key=lambda e: e.get("start_time_ns") or 0)
    steps = []
    for i, e in enumerate(candidates):
        attrs = e.get("attributes") or {}
        kind = e["kind"]
        if kind == A.LLM:
            name = str(attrs.get(A.LLM_MODEL, ""))
            inputs = {k: attrs[k] for k in (A.LLM_MESSAGES, A.LLM_PARAMS) if k in attrs}
        else:
            name = str(attrs.get(A.TOOL_NAME, ""))
            inputs = {k: attrs[k] for k in (A.TOOL_ARGS,) if k in attrs}
        error = None
        if e.get("status") == "error":
            error = (str(attrs.get(A.ERROR_TYPE, "Exception")), str(attrs.get(A.ERROR_MESSAGE, "")))
        steps.append(
            Step(
                index=i,
                kind=kind,
                name=name,
                span_id=e["span_id"],
                inputs=inputs,
                output=attrs.get(_OUTPUT_KEY[kind]),
                error=error,
                truncated=dict(e.get("truncated") or {}),
            )
        )
    return steps


@dataclass(frozen=True)
class Recording:
    """The replayable steps of one recorded run, as returned by ``GET /v1/runs/{id}``."""

    run_id: str
    name: str | None
    steps: list[Step]
    run: dict[str, Any] = field(repr=False)

    @classmethod
    def from_run(cls, run: dict[str, Any]) -> Recording:
        try:
            return cls(
                run_id=str(run["id"]),
                name=run.get("name"),
                steps=_steps(list(run.get("events") or [])),
                run=run,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ReplayError(f"not a run detail document: {exc!r}") from exc

    @classmethod
    def fetch(
        cls,
        run_id: str,
        endpoint: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> Recording:
        """Load a run from an AgentFlight server. ``api_key`` defaults to the env variable.

        Call ``agentflight.flush()`` first if the run was just recorded in this process.
        """
        key = api_key if api_key is not None else os.environ.get("AGENTFLIGHT_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = f"{endpoint.rstrip('/')}/v1/runs/{run_id}"
        try:
            if client is not None:
                resp = client.get(url, headers=headers)
            else:
                resp = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ReplayError(f"could not fetch run {run_id}: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise ReplayError(f"could not fetch run {run_id}: HTTP {resp.status_code}")
        return cls.from_run(resp.json())

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        """Load a run saved with :meth:`save` (or a saved ``GET /v1/runs/{id}`` response)."""
        return cls.from_run(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.run, ensure_ascii=False), encoding="utf-8")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _rebuild_error(error: tuple[str, str]) -> BaseException:
    type_name, message = error
    cls = getattr(builtins, type_name, None)
    if isinstance(cls, type) and issubclass(cls, Exception):
        try:
            return cls(message)
        except Exception:
            pass
    return ReplayedError(type_name, message)


class ReplaySession:
    """Serves recorded outputs in order. Created by :func:`replay`."""

    mode = "replay"

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.cursor = 0  # number of model/tool calls seen so far
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        """Recorded steps the run hasn't reached (0 when the replay covered the run)."""
        return max(0, len(self.recording.steps) - self.cursor)

    def run_attributes(self) -> dict[str, Any]:
        return {A.REPLAY_SOURCE_RUN: self.recording.run_id, A.REPLAY_MODE: self.mode}

    def _check(self, step: Step, kind: str, name: str, inputs: dict[str, Any]) -> None:
        where = f"step {step.index}"
        if (step.kind, step.name) != (kind, name):
            raise ReplayDivergence(
                f"{where}: recorded {step.kind} {step.name!r}, got {kind} {name!r}"
            )
        for key, live in inputs.items():
            recorded = step.inputs.get(key)
            if live is None and key not in step.inputs:
                continue
            text, _ = sdk._encode(key, live)
            if key in step.truncated:
                same = isinstance(recorded, str) and text == recorded
            else:
                try:
                    same = _canonical(json.loads(text)) == _canonical(recorded)
                except ValueError:
                    same = False
            if not same:
                raise ReplayDivergence(f"{where} ({kind} {name!r}): {key} differs from recording")

    def _serve(self, step: Step) -> Any:
        if step.error is not None:
            raise _rebuild_error(step.error)
        output_key = _OUTPUT_KEY[step.kind]
        if output_key in step.truncated:
            raise ReplayError(f"step {step.index}: recorded {output_key} is truncated")
        return step.output

    def _take(self, kind: str, name: str, inputs: dict[str, Any]) -> tuple[str, Any, int]:
        """Pick what to do for the next call: ("cached", value, index) or ("live", ...)."""
        index = self.cursor
        self.cursor += 1
        steps = self.recording.steps
        if index >= len(steps):
            raise ReplayDivergence(
                f"step {index}: {kind} {name!r} was not in the recording "
                f"({len(steps)} recorded steps)"
            )
        step = steps[index]
        self._check(step, kind, name, inputs)
        return "cached", step, index

    def next(self, kind: str, name: str, inputs: dict[str, Any], span: Span) -> tuple[bool, Any]:
        """Called by the SDK for each model/tool call. Returns (served, value)."""
        with self._lock:
            source, payload, index = self._take(kind, name, inputs)
        sdk._safe(span.set_attribute, A.REPLAY_STEP, index)
        sdk._safe(span.set_attribute, A.REPLAY_SOURCE, source)
        if source == "live":
            return False, None
        if source == "override":
            return True, payload
        return True, self._serve(payload)


@contextmanager
def replay(recording: Recording) -> Iterator[ReplaySession]:
    """Replay ``recording``: recorded outputs are served and nothing is called live.

    Runs recorded inside the block carry ``agentflight.replay.*`` attributes, so a replay
    is itself recorded (as a new run) and can be compared with the original.
    """
    session = ReplaySession(recording)
    token = sdk._replay_session.set(session)
    try:
        yield session
    finally:
        sdk._replay_session.reset(token)
