"""OpenTelemetry span exporter that ships spans to an AgentFlight server."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from agentflight._spool import Spool

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

_RETRYABLE = frozenset({408, 425, 429})
"""Besides 5xx: statuses that may succeed later, so their spans are spooled."""


@dataclass
class _ExportState:
    """Shared by the requests of one ``export()`` call, spool replay included."""

    remaining: int
    deadline: float | None = None  # time.monotonic() value; None: no overall deadline
    aborted: str | None = None
    rejected: int = 0  # spans the server accepted the request for but didn't store
    rejected_why: str = ""

    def abort(self, reason: str) -> str:
        self.aborted = reason
        return reason


@dataclass
class _Outcome:
    """What happened to one batch: spans worth retrying later, and spans lost for good."""

    retry: list[dict[str, Any]] = field(default_factory=list)
    lost: int = 0
    why: str | None = None  # the first failure; "" when already logged (credentials)

    def fail(self, why: str) -> None:
        if self.why is None:
            self.why = why


Payload = dict[str, Any]


class AgentFlightExporter(SpanExporter):
    """POSTs batches of spans as JSON to ``{endpoint}/v1/spans``.

    ``api_key`` is sent as ``Authorization: Bearer <key>``. Auth failures are logged once
    (without the key) and never raised.

    If the server answers ``413`` (batch over its body cap), the batch is split in half
    and each half retried, so only spans that are too big on their own are dropped. One
    ``export()`` makes at most ``max_attempts`` requests, and stops at the first failure
    that isn't a 413. Each export that drops spans logs one warning.

    With a ``spool``, spans that weren't stored because of a failure that may clear up
    (transport error, timeout, 5xx, 408/425/429, credentials, attempt cap, deadline) are
    written to disk instead of dropped, and every later ``export()`` first sends what is
    spooled, oldest first, within the same attempt cap. Spans the server rejects for good
    (a single span over the body cap, other 4xx) are not spooled. Spans the server accepted
    the request for but didn't store (``rejected`` in its ``202`` reply, from server-side
    redaction in ``reject`` mode) are logged in one warning per export, and neither retried
    nor spooled.

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
        spool: Spool | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._url = endpoint.rstrip("/") + INGEST_PATH
        self._auth_warned = False
        self._max_attempts = max_attempts
        self._timeout = timeout
        self.spool = spool
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = dict(headers or {})
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def __repr__(self) -> str:
        return f"AgentFlightExporter(url={self._url!r})"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._export(spans, deadline_s=None, replay=True)

    def export_within(self, spans: Sequence[ReadableSpan], deadline_s: float) -> SpanExportResult:
        """Like ``export``, but give up after ``deadline_s`` seconds in total.

        Each request's timeouts are capped to what is left of the deadline, and no request
        starts after it has passed. Used for run spans, which skip the batch queue. It
        sends ``spans`` before anything spooled, and replays the spool only once they are
        stored, within the same deadline.
        """
        return self._export(spans, deadline_s=deadline_s, replay=False)

    def _export(
        self, spans: Sequence[ReadableSpan], *, deadline_s: float | None, replay: bool
    ) -> SpanExportResult:
        # Never raise: OTel processors log exporter exceptions, but a custom processor might
        # not, and export failures must never reach agent code.
        try:
            deadline = None if deadline_s is None else time.monotonic() + deadline_s
            state = _ExportState(remaining=self._max_attempts, deadline=deadline)
            if replay and self.spool is not None:
                self._replay(self.spool, state)
            items = [span_to_dict(s) for s in spans]
            outcome = _Outcome()
            self._send(items, state, outcome)
            if not replay and outcome.why is None and self.spool is not None:
                self._replay(self.spool, state)
            if state.rejected:
                # The server answered 202 but didn't store these (its redaction policy
                # failed on them). Retrying would fail the same way: not retried, not spooled.
                logger.warning(
                    "agentflight: the server rejected %d spans (not stored, not retried): %s",
                    state.rejected,
                    state.rejected_why or "no reason given",
                )
        except Exception as exc:
            logger.warning("agentflight: failed to export %d spans: %r", len(spans), exc)
            return SpanExportResult.FAILURE
        if outcome.why is None:
            return SpanExportResult.SUCCESS
        spooled = 0
        if outcome.retry:
            if self.spool is not None and self.spool.put(outcome.retry):
                spooled = len(outcome.retry)
            else:
                outcome.lost += len(outcome.retry)
        if outcome.why:  # "": already reported (credentials)
            logger.warning(
                "agentflight: failed to export %d of %d spans: %s%s",
                outcome.lost + spooled,
                len(spans),
                outcome.why,
                f" ({spooled} spooled to retry later)" if spooled else "",
            )
        return SpanExportResult.FAILURE

    def _replay(self, spool: Spool, state: _ExportState) -> None:
        """Send spooled batches, oldest first, until one fails or the budget runs out."""
        with closing(spool.replay()) as batches:
            for f, items in batches:
                if state.aborted is not None or state.remaining <= 0:
                    break
                outcome = _Outcome()
                self._send(items, state, outcome)
                spool.settle(f, len(items), outcome.retry)
                if outcome.lost:
                    logger.warning(
                        "agentflight: dropped %d spooled spans: %s", outcome.lost, outcome.why
                    )

    def _send(self, items: list[Payload], state: _ExportState, out: _Outcome) -> None:
        """POST ``items``, splitting on 413; record what happened in ``out``.

        The first failure other than 413 (timeout, transport error, 5xx, credentials, ...)
        aborts the rest of the export: the halves still waiting are not sent, since they
        would most likely fail the same way. They go to ``out.retry``, like the spans of
        any failure that may clear up; spans the server rejects for good count as lost.
        """
        if state.aborted is not None:
            out.fail(state.aborted)
            out.retry.extend(items)
            return
        if state.remaining <= 0:
            out.fail(f"gave up after {self._max_attempts} requests")
            out.retry.extend(items)
            return
        extra: dict[str, Any] = {}
        if state.deadline is not None:
            left = state.deadline - time.monotonic()
            if left <= 0:
                out.fail(state.abort("deadline passed"))
                out.retry.extend(items)
                return
            # An injected client keeps its own timeouts, capped to the deadline.
            extra["timeout"] = min(left, self._timeout) if self._owns_client else left
        state.remaining -= 1
        try:
            response = self._client.post(
                self._url, json={"spans": items}, headers=self._headers, **extra
            )
        except Exception as exc:
            out.fail(state.abort(repr(exc)))
            out.retry.extend(items)
            return
        code = response.status_code
        if code in (401, 403):
            if not self._auth_warned:
                self._auth_warned = True
                logger.warning(
                    "agentflight: server rejected credentials (HTTP %d); spans are being "
                    "dropped. Check AGENTFLIGHT_API_KEY.",
                    code,
                )
            out.fail(state.abort(""))
            out.retry.extend(items)
            return
        if code == 413:
            if len(items) == 1:
                out.fail("span is larger than the server's body cap (HTTP 413)")
                out.lost += 1
                return
            half = len(items) // 2
            self._send(items[:half], state, out)
            self._send(items[half:], state, out)
            return
        if 200 <= code < 300:
            self._note_rejected(response, state)
            return
        reason = state.abort(f"HTTP {code} from {self._url}")
        out.fail(reason)
        if code >= 500 or code in _RETRYABLE:
            out.retry.extend(items)
        else:
            out.lost += len(items)

    @staticmethod
    def _note_rejected(response: httpx.Response, state: _ExportState) -> None:
        try:
            body = response.json()
            rejected = int(body.get("rejected") or 0)
            message = body.get("error_message")
        except Exception:  # not JSON, or not our shape: nothing was reported
            return
        if rejected > 0:
            state.rejected += rejected
            if not state.rejected_why and isinstance(message, str):
                state.rejected_why = message

    def shutdown(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                logger.warning("agentflight: error closing exporter client", exc_info=True)
