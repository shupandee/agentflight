"""OTLP/HTTP trace ingest: ``POST /v1/traces`` for any OpenTelemetry SDK or Collector.

Requests are ``ExportTraceServiceRequest`` messages, as protobuf (``application/x-protobuf``)
or OTLP JSON (``application/json``), optionally gzip-compressed. Each span becomes an event
of the run named by its trace id, exactly like spans sent to ``/v1/spans``:

* Attributes are kept as they are (structured values become JSON). The resource's
  attributes are added to every span as ``otel.resource.<key>``, the instrumentation scope
  as ``otel.scope.name``/``otel.scope.version``, the span kind as ``otel.span_kind`` and
  links as ``otel.links``.
* Spans that already carry ``agentflight.*`` attributes (the AgentFlight SDK behind an
  OTLP exporter) are stored as they are.
* Other spans with GenAI semantic-convention attributes (``gen_ai.*``) get the matching
  AgentFlight fields added (kind ``llm`` or ``tool``, model, token counts, tool name), so
  run totals and filters work; the ``gen_ai.*`` attributes stay too.
* A span with no parent closes its run (name, start, end, status), keeping its own kind.

Spans that can't be stored (bad ids, over a size limit) are rejected one by one and
reported in ``partial_success``; the rest of the batch is stored.
"""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Literal

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from pydantic import ValidationError

from agentflight import attributes as A
from agentflight.server.limits import span_problem
from agentflight.server.schemas import SpanEventIn, SpanIn
from agentflight.server.settings import Settings

PROTOBUF = "application/x-protobuf"
JSON = "application/json"

# google.rpc.Code values used in error bodies (google.rpc.Status)
INVALID_ARGUMENT = 3
PERMISSION_DENIED = 7
RESOURCE_EXHAUSTED = 8
UNAVAILABLE = 14
UNAUTHENTICATED = 16

_STATUS: dict[int, Literal["ok", "error"]] = {
    Status.STATUS_CODE_OK: "ok",
    Status.STATUS_CODE_ERROR: "error",
}
_SPAN_KIND = {
    Span.SPAN_KIND_INTERNAL: "internal",
    Span.SPAN_KIND_SERVER: "server",
    Span.SPAN_KIND_CLIENT: "client",
    Span.SPAN_KIND_PRODUCER: "producer",
    Span.SPAN_KIND_CONSUMER: "consumer",
}
# Resource attributes that also become the run's metadata (filterable with meta=key:value).
RUN_METADATA_KEYS = (
    "service.name",
    "service.namespace",
    "service.version",
    "deployment.environment",
    "deployment.environment.name",
)
_LLM_OPERATIONS = frozenset({"chat", "text_completion", "generate_content", "embeddings"})
_MAX_ERRORS_REPORTED = 5


class OTLPError(Exception):
    """A request that fails as a whole, answered with ``status`` and a google.rpc.Status."""

    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def request_format(content_type: str | None) -> str | None:
    """``PROTOBUF``, ``JSON``, or None for a content type OTLP/HTTP doesn't define."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media if media in (PROTOBUF, JSON) else None


def decompress(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    """Undo ``Content-Encoding`` (none or gzip), never producing more than ``max_bytes``."""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        return body
    if enc != "gzip":
        raise OTLPError(415, INVALID_ARGUMENT, f"unsupported Content-Encoding {encoding!r}")
    out = bytearray()
    data = body
    try:
        while data:  # a gzip stream can have several members
            inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            chunk = inflater.decompress(data, max_bytes + 1 - len(out))
            out += chunk
            # Stop as soon as the output would pass the cap: gzip bombs never inflate.
            if len(out) > max_bytes:
                raise OTLPError(
                    413, RESOURCE_EXHAUSTED, f"decompressed body exceeds {max_bytes} bytes"
                )
            if inflater.unconsumed_tail:
                raise OTLPError(
                    413, RESOURCE_EXHAUSTED, f"decompressed body exceeds {max_bytes} bytes"
                )
            if not inflater.eof:
                raise OTLPError(400, INVALID_ARGUMENT, "truncated gzip body")
            data = inflater.unused_data
    except zlib.error as exc:
        raise OTLPError(400, INVALID_ARGUMENT, f"invalid gzip body: {exc}") from None
    return bytes(out)


def _hex_to_b64(value: Any) -> Any:
    """OTLP JSON encodes ids as hex; protobuf's JSON mapping expects base64."""
    if not isinstance(value, str):
        return value
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raw = b"\x00"  # not hex: becomes an id of the wrong length, so the span is rejected
    return base64.b64encode(raw).decode()


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _fix_json_ids(doc: dict[str, Any]) -> None:
    for rs in _dicts(doc.get("resourceSpans")):
        for ss in _dicts(rs.get("scopeSpans")):
            for span in _dicts(ss.get("spans")):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = _hex_to_b64(span[key])
                for link in _dicts(span.get("links")):
                    for key in ("traceId", "spanId"):
                        if key in link:
                            link[key] = _hex_to_b64(link[key])


def parse(body: bytes, fmt: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    if fmt == PROTOBUF:
        try:
            request.ParseFromString(body)
        except (DecodeError, ValueError, RuntimeError) as exc:
            raise OTLPError(400, INVALID_ARGUMENT, f"invalid protobuf body: {exc}") from None
        return request
    try:
        doc = json.loads(body)
        if not isinstance(doc, dict):
            raise ValueError("expected a JSON object")
        _fix_json_ids(doc)
        json_format.ParseDict(doc, request, ignore_unknown_fields=True)
    except (ValueError, TypeError, RecursionError, json_format.ParseError) as exc:
        raise OTLPError(400, INVALID_ARGUMENT, f"invalid OTLP JSON body: {exc}") from None
    return request


def any_value(value: AnyValue) -> Any:
    """An OTLP ``AnyValue`` as plain JSON-compatible Python."""
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: any_value(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return base64.b64encode(value.bytes_value).decode()
    return getattr(value, which)


def _attrs(items: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    kv: KeyValue
    for kv in items:
        out[kv.key] = any_value(kv.value)
    return out


def _first(attrs: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if attrs.get(key) is not None:
            return attrs[key]
    return None


def genai_fields(attrs: dict[str, Any]) -> dict[str, Any]:
    """AgentFlight attributes implied by GenAI semantic-convention attributes."""
    operation = attrs.get("gen_ai.operation.name")
    if operation == "execute_tool" or (
        operation is None and attrs.get("gen_ai.tool.name") is not None
    ):
        out: dict[str, Any] = {A.KIND: A.TOOL}
        if attrs.get("gen_ai.tool.name") is not None:
            out[A.TOOL_NAME] = str(attrs["gen_ai.tool.name"])
        return out
    is_model_call = operation in _LLM_OPERATIONS or (
        operation is None
        and _first(attrs, "gen_ai.request.model", "gen_ai.system", "gen_ai.provider.name")
        is not None
    )
    if not is_model_call:
        return {}
    out = {A.KIND: A.LLM}
    model = _first(attrs, "gen_ai.response.model", "gen_ai.request.model")
    if model is not None:
        out[A.LLM_MODEL] = str(model)
    tokens_in = _first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
    if tokens_in is not None:
        out[A.LLM_TOKENS_IN] = tokens_in
    tokens_out = _first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
    if tokens_out is not None:
        out[A.LLM_TOKENS_OUT] = tokens_out
    return out


def _id(raw: bytes, size: int) -> str | None:
    """Hex id, or None if missing, all zeros or the wrong length."""
    if len(raw) != size or not any(raw):
        return None
    return raw.hex()


@dataclass
class Converted:
    spans: list[SpanIn] = field(default_factory=list)
    roots: set[tuple[str, str]] = field(default_factory=set)
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def reject(self, why: str) -> None:
        self.rejected += 1
        if len(self.errors) < _MAX_ERRORS_REPORTED:
            self.errors.append(why)

    @property
    def error_message(self) -> str:
        if not self.rejected:
            return ""
        more = self.rejected - len(self.errors)
        return "; ".join(self.errors) + (f"; and {more} more" if more > 0 else "")


def span_count(request: ExportTraceServiceRequest) -> int:
    return sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)


def convert(request: ExportTraceServiceRequest, settings: Settings) -> Converted:
    out = Converted()
    for rs in request.resource_spans:
        resource = _attrs(rs.resource.attributes)
        resource_attrs = {f"otel.resource.{k}": v for k, v in resource.items()}
        metadata = {k: resource[k] for k in RUN_METADATA_KEYS if isinstance(resource.get(k), str)}
        for ss in rs.scope_spans:
            scope: dict[str, Any] = {}
            if ss.scope.name:
                scope["otel.scope.name"] = ss.scope.name
            if ss.scope.version:
                scope["otel.scope.version"] = ss.scope.version
            for span in ss.spans:
                _convert_span(span, resource_attrs, scope, metadata, settings, out)
    return out


def _validation_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error.get("loc", ()))
    msg = str(error.get("msg", "invalid"))
    return f"{where}: {msg}" if where else msg


def _convert_span(
    span: Span,
    resource_attrs: dict[str, Any],
    scope: dict[str, Any],
    metadata: dict[str, Any],
    settings: Settings,
    out: Converted,
) -> None:
    trace_id = _id(span.trace_id, 16)
    span_id = _id(span.span_id, 8)
    label = f"span {span.name[:64]!r}"
    if trace_id is None:
        out.reject(f"{label}: invalid trace_id")
        return
    if span_id is None:
        out.reject(f"{label}: invalid span_id")
        return
    parent = _id(span.parent_span_id, 8) if span.parent_span_id else None
    if span.parent_span_id and parent is None and any(span.parent_span_id):
        out.reject(f"{label}: invalid parent_span_id")
        return
    own = _attrs(span.attributes)
    from_agentflight = A.KIND in own
    attrs = {**resource_attrs, **scope, **own}
    if span.kind in _SPAN_KIND:
        attrs["otel.span_kind"] = _SPAN_KIND[span.kind]
    if span.links:
        attrs["otel.links"] = [
            {
                "trace_id": link.trace_id.hex(),
                "span_id": link.span_id.hex(),
                "attributes": _attrs(link.attributes),
            }
            for link in span.links
        ]
    if not from_agentflight:
        attrs.update(genai_fields(attrs))
        if parent is None and metadata:
            attrs.setdefault(A.RUN_METADATA, dict(metadata))
    try:
        events = [
            SpanEventIn(
                name=event.name,
                time_ns=event.time_unix_nano,
                attributes=_attrs(event.attributes),
            )
            for event in span.events
        ]
    except ValidationError as exc:
        out.reject(f"{label}: event {_validation_message(exc)}")
        return
    try:
        item = SpanIn(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent,
            name=span.name,
            start_time_ns=span.start_time_unix_nano,
            end_time_ns=span.end_time_unix_nano or None,
            status=_STATUS.get(span.status.code) or "unset",
            status_message=span.status.message or None,
            attributes=attrs,
            events=events,
        )
    except ValidationError as exc:
        out.reject(f"{label}: {_validation_message(exc)}")
        return
    problem = span_problem(item, settings)
    if problem is not None:
        out.reject(f"{label}: {problem}")
        return
    out.spans.append(item)
    if parent is None and not from_agentflight:
        out.roots.add((trace_id, span_id))


def encode_response(fmt: str, converted: Converted) -> bytes:
    response = ExportTraceServiceResponse()
    if converted.rejected:
        response.partial_success.rejected_spans = converted.rejected
        response.partial_success.error_message = converted.error_message
    if fmt == PROTOBUF:
        return bytes(response.SerializeToString())
    return json.dumps(json_format.MessageToDict(response)).encode()


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_status(fmt: str, code: int, message: str) -> bytes:
    """A google.rpc.Status (``code`` = 1, ``message`` = 2), as OTLP/HTTP error bodies are."""
    if fmt == PROTOBUF:
        text = message.encode()
        return b"\x08" + _varint(code) + b"\x12" + _varint(len(text)) + text
    return json.dumps({"code": code, "message": message}).encode()
