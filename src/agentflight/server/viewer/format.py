"""Viewer helpers: filter-form parsing, timeline geometry and template filters.

Template filters return plain strings; Jinja2's autoescaping escapes them on output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from agentflight import attributes as A
from agentflight.server import service

_META_KEY = re.compile(service.META_KEY_PATTERN)
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
NS = 1_000_000_000


@dataclass
class FilterForm:
    """The run-list filter form: raw values to re-fill the form, parsed filters, errors."""

    values: dict[str, str] = field(default_factory=dict)
    filters: service.RunFilters = field(default_factory=service.RunFilters)
    offset: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_time(raw: str) -> int:
    """A ``datetime-local`` value (or ISO 8601), read as UTC unless it has an offset."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp()) * NS + parsed.microsecond * 1000


def parse_filters(params: Mapping[str, str]) -> FilterForm:
    names = (
        "status",
        "name",
        "model",
        "tool",
        "has_errors",
        "meta",
        "since",
        "until",
        "parent",
        "has_parent",
    )
    values = {k: str(params.get(k, "")).strip()[:255] for k in names}
    form = FilterForm(values=values)
    status = values["status"] or None
    if status and status not in service.STATUSES:
        form.errors.append(f"Unknown status {status!r}.")
        status = None
    has_errors = {"": None, "yes": True, "no": False}.get(values["has_errors"])
    parent = values["parent"].lower() or None
    if parent and not _RUN_ID.match(parent):
        form.errors.append("Parent run must be a 32-character run id.")
        parent = None
    has_parent = {"": None, "true": True, "false": False}.get(values["has_parent"])
    metadata: tuple[tuple[str, str], ...] = ()
    if values["meta"]:
        key, sep, value = values["meta"].partition(":")
        if sep and _META_KEY.match(key):
            metadata = ((key, value),)
        else:
            form.errors.append("Metadata filter must look like key:value.")
    times: dict[str, int | None] = {"since": None, "until": None}
    for name in times:
        if values[name]:
            try:
                times[name] = _parse_time(values[name])
            except ValueError:
                form.errors.append(f"{name.capitalize()} isn't a valid date and time.")
    try:
        form.offset = max(0, int(params.get("offset", "0")))
    except ValueError:
        form.offset = 0
    form.filters = service.RunFilters(
        status=status,
        name=values["name"] or None,
        since_ns=times["since"],
        until_ns=times["until"],
        has_errors=has_errors,
        model=values["model"] or None,
        tool=values["tool"] or None,
        metadata=metadata,
        parent_run_id=parent,
        has_parent=has_parent,
    )
    return form


def query_string(values: Mapping[str, str], **overrides: Any) -> str:
    merged = {k: v for k, v in values.items() if v}
    merged.update({k: str(v) for k, v in overrides.items() if v not in (None, "", 0)})
    return urlencode(merged)


@dataclass(frozen=True)
class Scale:
    start_ns: int
    span_ns: int

    def bar(self, event: Mapping[str, Any]) -> tuple[float, float]:
        """(x, width) of an event's bar on a 0-1000 axis."""
        start = event["start_time_ns"] - self.start_ns
        end = (event.get("end_time_ns") or event["start_time_ns"]) - self.start_ns
        x = min(1000.0, max(0.0, start * 1000 / self.span_ns))
        width = max(2.0, min(1000.0 - x, (end - start) * 1000 / self.span_ns))
        return round(x, 2), round(width, 2)


def timeline_scale(run: Mapping[str, Any]) -> Scale:
    """The run's time axis. Unfinished runs end at the latest event seen so far."""
    events = run.get("events") or []
    starts = [e["start_time_ns"] for e in events]
    ends = [e.get("end_time_ns") or e["start_time_ns"] for e in events]
    start = run.get("start_time_ns") or (min(starts) if starts else 0)
    end = run.get("end_time_ns") or (max(ends) if ends else start)
    return Scale(start_ns=start, span_ns=max(1, end - start))


def replay_info(root: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Replay/fork attributes recorded on the run span, if this run is a replay."""
    if not root:
        return None
    attrs = root.get("attributes") or {}
    if A.REPLAY_SOURCE_RUN not in attrs:
        return None
    return {
        "source_run": str(attrs.get(A.REPLAY_SOURCE_RUN)),
        "mode": attrs.get(A.REPLAY_MODE),
        "fork_step": attrs.get(A.REPLAY_FORK_STEP),
        "diverged_at": attrs.get(A.REPLAY_DIVERGED_AT),
        "divergence": attrs.get(A.REPLAY_DIVERGENCE),
    }


def utc(ns: int | None) -> str:
    if ns is None:
        return "—"
    moment = datetime.fromtimestamp(ns / NS, tz=UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d} UTC"


def duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1:
        return f"{ms:.3f} ms"
    if ms < 1000:
        return f"{ms:.1f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{int(minutes)} min {seconds:.0f} s"


def offset_ms(ns: int | None, start_ns: int) -> str:
    return "—" if ns is None else f"+{(ns - start_ns) / 1e6:,.1f} ms"


def number(value: int | float | None) -> str:
    return "—" if value is None else f"{value:,}"


def cost(value: float | None) -> str:
    if value is None:
        return "—"
    # Small amounts keep 6 decimals so they don't round to $0.0000.
    return f"${value:,.6f}" if abs(value) < 0.01 else f"${value:,.4f}"


def pretty(value: Any) -> str:
    """Payloads as indented JSON text. Escaped by the template, never marked safe."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(value)


FILTERS = {
    "utc": utc,
    "duration": duration,
    "offset_ms": offset_ms,
    "number": number,
    "cost": cost,
    "pretty": pretty,
    "qs": query_string,
}
