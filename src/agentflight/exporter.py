"""OpenTelemetry span exporter that ships spans to an AgentFlight server."""

from __future__ import annotations

import logging
from collections.abc import Sequence
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


class AgentFlightExporter(SpanExporter):
    """POSTs batches of spans as JSON to ``{endpoint}/v1/spans``.

    An ``httpx.Client`` can be injected (useful for tests or custom auth/TLS).
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8000",
        *,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = endpoint.rstrip("/") + INGEST_PATH
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = headers or {}

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # Never raise: OTel processors log exporter exceptions, but a custom processor might
        # not, and export failures must never reach agent code.
        try:
            payload = {"spans": [span_to_dict(s) for s in spans]}
            response = self._client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("agentflight: failed to export %d spans: %r", len(spans), exc)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                logger.warning("agentflight: error closing exporter client", exc_info=True)
