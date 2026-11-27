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

A fork replays the recording up to a chosen step and runs live from there, optionally
substituting that step's output::

    with agentflight.fork(recording, at=2, output="-5C, snow"):
        answer = my_agent(question)  # steps 0-1 cached, step 2 returns the new output
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from opentelemetry.trace import Span

from agentflight import attributes as A
from agentflight import sdk

logger = logging.getLogger("agentflight")

OnDivergence = Literal["raise", "live"]

STEP_KINDS = (A.LLM, A.TOOL)
_NO_OUTPUT: Any = object()
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

    def __init__(self, recording: Recording, on_divergence: OnDivergence = "raise") -> None:
        if on_divergence not in ("raise", "live"):
            raise ValueError(f"on_divergence must be 'raise' or 'live', not {on_divergence!r}")
        self.recording = recording
        self.on_divergence = on_divergence
        self.cursor = 0  # number of model/tool calls seen so far
        # With on_divergence="live": the first step that didn't match, and why.
        self.diverged_at: int | None = None
        self.divergence: str | None = None
        self._runs: list[Span] = []  # open runs, innermost last
        self._lock = threading.Lock()

    def bind_run(self, span: Span) -> None:
        """Called by the SDK when a run opens, so a divergence can be noted on it."""
        self._runs.append(span)

    def unbind_run(self, span: Span) -> None:
        if span in self._runs:
            self._runs.remove(span)

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
        """Pick what to do for the next call: ("cached", step, index), ("override", value,
        index) or ("live", None, index)."""
        index = self.cursor
        self.cursor += 1
        if self.diverged_at is not None:
            return "live", None, index
        try:
            return self._plan(index, kind, name, inputs)
        except ReplayDivergence as exc:
            if self.on_divergence == "raise":
                raise
            self._diverge(index, str(exc))
            return "live", None, index

    def _plan(
        self, index: int, kind: str, name: str, inputs: dict[str, Any]
    ) -> tuple[str, Any, int]:
        return self._cached(index, kind, name, inputs)

    def _diverge(self, index: int, reason: str) -> None:
        self.diverged_at = index
        self.divergence = reason
        logger.warning(
            "agentflight: replay diverged (%s); running live from step %d", reason, index
        )
        span = self._runs[-1] if self._runs else None
        if span is not None:
            sdk._safe(span.set_attribute, A.REPLAY_DIVERGED_AT, index)
            sdk._safe(span.set_attribute, A.REPLAY_DIVERGENCE, reason)

    def _cached(
        self, index: int, kind: str, name: str, inputs: dict[str, Any]
    ) -> tuple[str, Any, int]:
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


class ForkSession(ReplaySession):
    """Replays steps before ``at``, then runs live. Created by :func:`fork`."""

    mode = "fork"

    def __init__(
        self,
        recording: Recording,
        at: int,
        output: Any = _NO_OUTPUT,
        on_divergence: OnDivergence = "raise",
    ) -> None:
        super().__init__(recording, on_divergence)
        count = len(recording.steps)
        if not 0 <= at <= count:
            raise ReplayError(f"fork step {at} out of range (recording has {count} steps)")
        if output is not _NO_OUTPUT and at == count:
            raise ReplayError(f"no step {at} to override (recording has {count} steps)")
        self.at = at
        self.output = output

    def run_attributes(self) -> dict[str, Any]:
        return {**super().run_attributes(), A.REPLAY_FORK_STEP: self.at}

    def _plan(
        self, index: int, kind: str, name: str, inputs: dict[str, Any]
    ) -> tuple[str, Any, int]:
        if index < self.at:
            return self._cached(index, kind, name, inputs)
        if index == self.at and self.output is not _NO_OUTPUT:
            step = self.recording.steps[index]
            if (step.kind, step.name) != (kind, name):
                raise ReplayDivergence(
                    f"step {index}: recorded {step.kind} {step.name!r}, got {kind} {name!r}"
                )
            return "override", self.output, index
        return "live", None, index


@contextmanager
def replay(
    recording: Recording, *, on_divergence: OnDivergence = "raise"
) -> Iterator[ReplaySession]:
    """Replay ``recording``: recorded outputs are served and nothing is called live.

    Runs recorded inside the block carry ``agentflight.replay.*`` attributes, so a replay
    is itself recorded (as a new run) and can be compared with the original.

    ``on_divergence="raise"`` (the default) raises :class:`ReplayDivergence` at the first
    call that doesn't match the recording. ``"live"`` instead stops serving recorded
    outputs at that step and runs it and every later call live; the step and reason are
    kept on the session (``diverged_at``, ``divergence``) and on the run span.
    """
    session = ReplaySession(recording, on_divergence)
    token = sdk._replay_session.set(session)
    try:
        yield session
    finally:
        sdk._replay_session.reset(token)


@contextmanager
def fork(
    recording: Recording,
    at: int,
    *,
    output: Any = _NO_OUTPUT,
    on_divergence: OnDivergence = "raise",
) -> Iterator[ForkSession]:
    """Replay ``recording`` up to step ``at``, then run live.

    Steps before ``at`` are served from the recording and must match it. Step ``at`` and
    everything after it run live, so changed code, prompts or arguments take effect there.
    With ``output=...``, step ``at`` isn't run: it returns that value instead (a tool
    result, or a model response in the form your code reads it), and live execution
    starts at the next step. ``at`` may equal the number of steps: the whole recording is
    replayed and any further calls run live. ``on_divergence`` works as in :func:`replay`
    for the replayed prefix (and the override step's kind and name).
    """
    session = ForkSession(recording, at, output, on_divergence)
    token = sdk._replay_session.set(session)
    try:
        yield session
    finally:
        sdk._replay_session.reset(token)
