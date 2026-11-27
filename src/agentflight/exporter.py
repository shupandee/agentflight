"""OpenTelemetry span exporter that ships spans to an AgentFlight server."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

INGEST_PATH = "/v1/spans"


def _hex(value: int, width: int) -> str:
    return format(value, f"0{width}x")


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """Convert a finished span into the JSON shape the ingest API accepts."""
    ctx = span.get_span_context()
    if ctx is None:  # pragma: no cover - finished spans always have a context
        raise ValueError("span has no context")
    parent = span.parent
    status = span.status.status_code
    return {
        "trace_id": _hex(ctx.trace_id, 32),
        "span_id": _hex(ctx.span_id, 16),
        "parent_span_id": _hex(parent.span_id, 16) if parent is not None else None,
        "name": span.name,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "status": {StatusCode.ERROR: "error", StatusCode.OK: "ok"}.get(status, "unset"),
        "status_message": span.status.description,
        "attributes": dict(span.attributes or {}),
        "events": [
            {
                "name": event.name,
                "time_ns": event.timestamp,
                "attributes": dict(event.attributes or {}),
            }
            for event in span.events
        ],
    }


DEFAULT_MAX_ATTEMPTS = 32
"""HTTP requests one ``export()`` may make while splitting batches the server rejects
with 413. Isolating one oversized span in a 512-span batch takes 19."""


@dataclass
class _ExportState:
    """Shared by the requests of one ``export()`` call."""

    remaining: int
    deadline: float | None = None  # time.monotonic() value; None: no overall deadline
    aborted: str | None = None

    def abort(self, reason: str) -> str:
        self.aborted = reason
        return reason


class AgentFlightExporter(SpanExporter):
    """POSTs batches of spans as JSON to ``{endpoint}/v1/spans``.

    ``api_key`` is sent as ``Authorization: Bearer <key>``. Auth failures are logged once
    (without the key) and never raised.

    If the server answers ``413`` (batch over its body cap), the batch is split in half
    and each half retried, so only spans that are too big on their own are dropped. One
    ``export()`` makes at most ``max_attempts`` requests, and stops at the first failure
    that isn't a 413; what is left unsent is dropped. Each export that drops spans logs one
    warning.

    An ``httpx.Client`` can be injected (useful for tests or custom auth/TLS).
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8000",
        *,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._url = endpoint.rstrip("/") + INGEST_PATH
        self._auth_warned = False
        self._max_attempts = max_attempts
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = dict(headers or {})
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def __repr__(self) -> str:
        return f"AgentFlightExporter(url={self._url!r})"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._export(spans, deadline_s=None)

    def export_within(self, spans: Sequence[ReadableSpan], deadline_s: float) -> SpanExportResult:
        """Like ``export``, but give up after ``deadline_s`` seconds in total.

        Each request's timeouts are capped to what is left of the deadline, and no request
        starts after it has passed. Used for run spans, which skip the batch queue.
        """
        return self._export(spans, deadline_s=deadline_s)

    def _export(
        self, spans: Sequence[ReadableSpan], *, deadline_s: float | None
    ) -> SpanExportResult:
        # Never raise: OTel processors log exporter exceptions, but a custom processor might
        # not, and export failures must never reach agent code.
        try:
            deadline = None if deadline_s is None else time.monotonic() + deadline_s
            state = _ExportState(remaining=self._max_attempts, deadline=deadline)
            dropped, reason = self._send(list(spans), state)
        except Exception as exc:
            logger.warning("agentflight: failed to export %d spans: %r", len(spans), exc)
            return SpanExportResult.FAILURE
        if reason is None:
            return SpanExportResult.SUCCESS
        if reason:  # "": already reported (credentials)
            logger.warning(
                "agentflight: failed to export %d of %d spans: %s", dropped, len(spans), reason
            )
        return SpanExportResult.FAILURE

    def _send(self, spans: list[ReadableSpan], state: _ExportState) -> tuple[int, str | None]:
        """POST ``spans``, splitting on 413.

        Returns (spans dropped, why): ``why`` is None on success and ``""`` when the failure
        was already logged. The first failure other than 413 (timeout, transport error,
        5xx, credentials, ...) aborts the rest of the export: the halves still waiting are
        dropped without being sent, since they would most likely fail the same way.
        """
        if state.aborted is not None:
            return len(spans), state.aborted
        if state.remaining <= 0:
            return len(spans), f"gave up after {self._max_attempts} requests"
        extra: dict[str, Any] = {}
        if state.deadline is not None:
            left = state.deadline - time.monotonic()
            if left <= 0:
                return len(spans), state.abort("deadline passed")
            # An injected client keeps its own timeouts, capped to the deadline.
            extra["timeout"] = min(left, self._timeout) if self._owns_client else left
        state.remaining -= 1
        try:
            payload = {"spans": [span_to_dict(s) for s in spans]}
            response = self._client.post(self._url, json=payload, headers=self._headers, **extra)
        except Exception as exc:
            return len(spans), state.abort(repr(exc))
        if response.status_code in (401, 403):
            if not self._auth_warned:
                self._auth_warned = True
                logger.warning(
                    "agentflight: server rejected credentials (HTTP %d); spans are being "
                    "dropped. Check AGENTFLIGHT_API_KEY.",
                    response.status_code,
                )
            return len(spans), state.abort("")
        if response.status_code == 413:
            if len(spans) == 1:
                return 1, "span is larger than the server's body cap (HTTP 413)"
            half = len(spans) // 2
            first, why_first = self._send(spans[:half], state)
            second, why_second = self._send(spans[half:], state)
            return first + second, why_first if why_first is not None else why_second
        try:
            response.raise_for_status()
        except Exception as exc:
            return len(spans), state.abort(repr(exc))
        return 0, None

    def shutdown(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                logger.warning("agentflight: error closing exporter client", exc_info=True)
