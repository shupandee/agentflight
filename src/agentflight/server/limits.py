"""Request size limits for ingest, so one bad client can't fill the database."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentflight.server.schemas import SpanIn
from agentflight.server.settings import Settings


class PayloadTooLarge(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=413, detail=detail)


class BodySizeLimit:
    """Rejects request bodies over ``max_bytes`` with 413.

    Checks ``Content-Length`` up front, and counts streamed (chunked) bodies as they are
    read, so a missing or wrong header doesn't get around the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        detail = f"request body exceeds {self.max_bytes} bytes"
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    await _reject(send, detail, scope)
                    return
        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # FastAPI re-raises HTTPExceptions from body parsing as they are.
                    raise PayloadTooLarge(detail)
            return message

        await self.app(scope, counting_receive, send)


async def _reject(send: Send, detail: str, scope: Scope) -> None:
    body = json.dumps({"detail": detail}).encode()
    media = b"application/json"
    if scope.get("path") == "/v1/traces":  # OTLP clients expect a google.rpc.Status
        from agentflight.server import otlp

        headers = dict(scope.get("headers", []))
        fmt = otlp.request_format(headers.get(b"content-type", b"").decode("latin-1"))
        fmt = fmt or otlp.JSON
        body, media = otlp.encode_status(fmt, otlp.RESOURCE_EXHAUSTED, detail), fmt.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", media),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return 0
    return len(json.dumps(value, ensure_ascii=False, default=str))


def span_problem(span: SpanIn, settings: Settings) -> str | None:
    """Why this span breaks a configured size limit, or None if it doesn't."""
    max_chars = settings.max_attribute_chars
    if len(span.attributes) > settings.max_attributes_per_span:
        return f"{len(span.attributes)} attributes; the limit is {settings.max_attributes_per_span}"
    if len(span.events) > settings.max_events_per_span:
        return f"{len(span.events)} events; the limit is {settings.max_events_per_span}"
    if span.status_message is not None and len(span.status_message) > max_chars:
        return f"status_message exceeds {max_chars} characters"
    for key, value in span.attributes.items():
        if len(key) > 255 or _size(value) > max_chars:
            return f"attribute {key[:255]!r} exceeds the size limit"
    for event in span.events:
        if len(event.attributes) > settings.max_attributes_per_span:
            return f"event {event.name!r} has too many attributes"
        for key, value in event.attributes.items():
            if len(key) > 255 or _size(value) > max_chars:
                return f"event {event.name!r} attribute {key[:255]!r} exceeds the size limit"
    return None


def check_spans(spans: list[SpanIn], settings: Settings) -> None:
    """Raise 413 if a batch breaks a configured limit. Nothing is stored in that case."""
    if len(spans) > settings.max_spans_per_batch:
        raise PayloadTooLarge(
            f"{len(spans)} spans in one batch; the limit is {settings.max_spans_per_batch}"
        )
    for i, span in enumerate(spans):
        problem = span_problem(span, settings)
        if problem is not None:
            raise PayloadTooLarge(f"span {i} ({span.span_id}): {problem}")
