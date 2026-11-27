"""OTLP/HTTP ingest: POST /v1/traces with protobuf or JSON, optionally gzip."""

from __future__ import annotations

import base64
import gzip
import json
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import OperationalError

from agentflight.server import create_app, otlp, service
from agentflight.server.models import Event, Run
from agentflight.server.settings import Settings, parse_api_keys
from tests.conftest import TEST_KEY

INGEST_KEY = "ingest-key-0123456789abcdef0123456789"
READ_KEY = "read-key-0123456789abcdef0123456789"
PB = {"Content-Type": "application/x-protobuf"}
JS = {"Content-Type": "application/json"}


def _settings(**overrides: Any) -> Settings:
    keys = parse_api_keys(f"{INGEST_KEY}:ingest,{READ_KEY}:read")
    return Settings(api_key=TEST_KEY, scoped_keys=keys, **overrides)


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    with TestClient(
        create_app(engine, _settings()), headers={"Authorization": f"Bearer {TEST_KEY}"}
    ) as c:
        yield c


def _record(build: Any, service_name: str = "support-bot") -> list[ReadableSpan]:
    """Spans recorded by the plain OpenTelemetry SDK (no AgentFlight code involved)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment.name": "staging"}
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    build(provider.get_tracer("my.instrumentation", "2.1"))
    provider.shutdown()
    return list(exporter.get_finished_spans())


def _agent_trace(tracer: Any) -> None:
    with tracer.start_as_current_span("support-agent", kind=SpanKind.SERVER) as root:
        root.set_attribute("customer.tier", "gold")
        with tracer.start_as_current_span("chat gpt-4o") as llm:
            llm.set_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": "gpt-4o",
                    "gen_ai.response.model": "gpt-4o-2024-08-06",
                    "gen_ai.usage.input_tokens": 120,
                    "gen_ai.usage.output_tokens": 30,
                }
            )
            llm.add_event("gen_ai.choice", {"index": 0})
        with tracer.start_as_current_span("execute_tool lookup_order") as tool:
            tool.set_attributes(
                {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order"}
            )
        with tracer.start_as_current_span("chat gpt-4o-mini") as llm2:
            llm2.set_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-4o-mini",
                    "gen_ai.usage.input_tokens": 50,
                    "gen_ai.usage.output_tokens": 5,
                }
            )


def _request(spans: Sequence[ReadableSpan]) -> ExportTraceServiceRequest:
    request: ExportTraceServiceRequest = encode_spans(spans)
    return request


def _protobuf(spans: Sequence[ReadableSpan]) -> bytes:
    return bytes(_request(spans).SerializeToString())


def _hexify(doc: Any) -> Any:
    """OTLP/JSON writes trace and span ids as hex, not protobuf JSON's base64."""
    if isinstance(doc, list):
        return [_hexify(v) for v in doc]
    if isinstance(doc, dict):
        return {
            k: (
                base64.b64decode(v).hex()
                if k in {"traceId", "spanId", "parentSpanId"}
                else _hexify(v)
            )
            for k, v in doc.items()
        }
    return doc


def _json(spans: Sequence[ReadableSpan]) -> bytes:
    doc = json_format.MessageToDict(_request(spans))
    return json.dumps(_hexify(doc)).encode()


def _run_id(spans: Sequence[ReadableSpan]) -> str:
    ctx = spans[0].context
    assert ctx is not None
    return f"{ctx.trace_id:032x}"


def _response(resp: Any) -> ExportTraceServiceResponse:
    out = ExportTraceServiceResponse()
    out.ParseFromString(resp.content)
    return out


def _status(resp: Any) -> tuple[int, str]:
    """Decode the google.rpc.Status protobuf body of an OTLP error response."""
    data, i, code, message = resp.content, 0, 0, ""
    while i < len(data):
        tag = data[i]
        i += 1
        n = shift = 0
        while True:
            b = data[i]
            i += 1
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        if tag == 0x08:
            code = n
        elif tag == 0x12:
            message = data[i : i + n].decode()
            i += n
    return code, message


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        runs = conn.execute(select(func.count()).select_from(Run)).scalar_one()
        events = conn.execute(select(func.count()).select_from(Event)).scalar_one()
    return runs, events


def test_protobuf_trace_becomes_a_run(api: TestClient) -> None:
    spans = _record(_agent_trace)
    resp = api.post("/v1/traces", content=_protobuf(spans), headers=PB)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-protobuf"
    assert not _response(resp).HasField("partial_success")

    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert run["name"] == "support-agent"
    assert run["status"] == "ok"  # root ended with an unset status: OTel's "ok"
    assert run["event_count"] == 4
    assert (run["llm_calls"], run["tool_calls"]) == (2, 1)
    assert (run["tokens_in"], run["tokens_out"]) == (170, 35)
    assert run["cost_usd"] is None  # no cost in the gen_ai conventions
    assert run["metadata"] == {
        "service.name": "support-bot",
        "deployment.environment.name": "staging",
    }
    by_name = {e["name"]: e for e in run["events"]}
    llm = by_name["chat gpt-4o"]
    assert llm["kind"] == "llm"
    assert llm["attributes"]["agentflight.llm.model"] == "gpt-4o-2024-08-06"
    assert by_name["execute_tool lookup_order"]["kind"] == "tool"
    assert llm["parent_span_id"] == by_name["support-agent"]["span_id"]
    assert llm["span_events"][0]["name"] == "gen_ai.choice"

    for query, total in [
        ("model=gpt-4o-mini", 1),
        ("tool=lookup_order", 1),
        ("meta=service.name:support-bot", 1),
        ("model=claude", 0),
    ]:
        assert api.get(f"/v1/runs?{query}").json()["total"] == total, query


def test_unknown_attributes_are_preserved(api: TestClient) -> None:
    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("other-root") as other:
            link = Link(other.get_span_context(), {"why": "retry"})
        with tracer.start_as_current_span("job", kind=SpanKind.CONSUMER, links=[link]) as s:
            s.set_attributes(
                {"custom.str": "x", "custom.list": [1, 2, 3], "custom.flag": True, "f": 1.5}
            )

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    job = next(s for s in spans if s.name == "job")
    assert job.context is not None
    run = api.get(f"/v1/runs/{job.context.trace_id:032x}").json()
    attrs = run["events"][0]["attributes"]
    assert attrs["custom.str"] == "x"
    assert attrs["custom.list"] == [1, 2, 3]
    assert attrs["custom.flag"] is True
    assert attrs["f"] == 1.5
    assert attrs["otel.resource.service.name"] == "support-bot"
    assert attrs["otel.resource.telemetry.sdk.language"] == "python"
    assert attrs["otel.scope.name"] == "my.instrumentation"
    assert attrs["otel.scope.version"] == "2.1"
    assert attrs["otel.span_kind"] == "consumer"
    assert attrs["otel.links"][0]["attributes"] == {"why": "retry"}
    assert run["events"][0]["kind"] == "span"


def test_any_value_kinds() -> None:
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue

    value = json_format.ParseDict(
        {
            "kvlistValue": {
                "values": [
                    {"key": "b", "value": {"bytesValue": base64.b64encode(b"\x00\x01").decode()}},
                    {"key": "i", "value": {"intValue": "9007199254740993"}},
                    {"key": "a", "value": {"arrayValue": {"values": [{"stringValue": "s"}, {}]}}},
                ]
            }
        },
        AnyValue(),
    )
    assert otlp.any_value(value) == {"b": "AAE=", "i": 9007199254740993, "a": ["s", None]}


def test_json_body(api: TestClient) -> None:
    spans = _record(_agent_trace)
    resp = api.post("/v1/traces", content=_json(spans), headers=JS)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {}
    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert (run["event_count"], run["tokens_in"], run["tokens_out"]) == (4, 170, 35)


def test_json_accepts_charset_and_unknown_fields(api: TestClient) -> None:
    spans = _record(_agent_trace)
    doc = json.loads(_json(spans))
    doc["resourceSpans"][0]["somethingNew"] = 1
    resp = api.post(
        "/v1/traces",
        content=json.dumps(doc),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("fmt", ["protobuf", "json"])
def test_gzip_body(api: TestClient, fmt: str) -> None:
    spans = _record(_agent_trace)
    body = _protobuf(spans) if fmt == "protobuf" else _json(spans)
    headers = {**(PB if fmt == "protobuf" else JS), "Content-Encoding": "gzip"}
    resp = api.post("/v1/traces", content=gzip.compress(body), headers=headers)
    assert resp.status_code == 200, resp.text
    assert api.get(f"/v1/runs/{_run_id(spans)}").json()["event_count"] == 4


def test_reingesting_the_same_batch_gives_identical_counts(api: TestClient, engine: Engine) -> None:
    spans = _record(_agent_trace)
    body = _protobuf(spans)
    assert api.post("/v1/traces", content=body, headers=PB).status_code == 200
    first = api.get(f"/v1/runs/{_run_id(spans)}").json()
    counts = _counts(engine)
    for _ in range(2):
        assert api.post("/v1/traces", content=body, headers=PB).status_code == 200
    again = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert _counts(engine) == counts == (1, 4)
    for key in ["event_count", "llm_calls", "tool_calls", "tokens_in", "tokens_out", "errors"]:
        assert again[key] == first[key], key
    # The JSON encoding of the same spans lands on the same rows too.
    assert api.post("/v1/traces", content=_json(spans), headers=JS).status_code == 200
    assert _counts(engine) == (1, 4)


def test_spans_split_across_batches(api: TestClient) -> None:
    spans = _record(_agent_trace)
    children = [s for s in spans if s.parent is not None]
    root = [s for s in spans if s.parent is None]
    assert api.post("/v1/traces", content=_protobuf(children), headers=PB).status_code == 200
    run_id = _run_id(spans)
    assert api.get(f"/v1/runs/{run_id}").json()["status"] == "running"
    assert api.post("/v1/traces", content=_protobuf(root), headers=PB).status_code == 200
    run = api.get(f"/v1/runs/{run_id}").json()
    assert (run["status"], run["name"], run["event_count"]) == ("ok", "support-agent", 4)


def test_root_error_status_fails_the_run(api: TestClient) -> None:
    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("agent") as root:
            with tracer.start_as_current_span("chat") as llm:
                llm.set_attribute("gen_ai.operation.name", "chat")
                llm.set_status(Status(StatusCode.ERROR, "rate limited"))
            root.set_status(Status(StatusCode.ERROR, "gave up"))

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert run["status"] == "error"
    assert run["error_events"] == 2
    msgs = {e["name"]: e["status_message"] for e in run["events"]}
    assert msgs == {"agent": "gave up", "chat": "rate limited"}


def test_single_genai_span_is_its_own_run(api: TestClient) -> None:
    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("chat claude") as s:
            s.set_attributes(
                {
                    "gen_ai.system": "anthropic",
                    "gen_ai.request.model": "claude-sonnet",
                    "gen_ai.usage.prompt_tokens": 7,
                    "gen_ai.usage.completion_tokens": 3,
                }
            )

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert (run["status"], run["llm_calls"], run["tokens_in"], run["tokens_out"]) == (
        "ok",
        1,
        7,
        3,
    )
    assert run["events"][0]["attributes"]["agentflight.llm.model"] == "claude-sonnet"


def test_agentflight_attributes_take_precedence(api: TestClient) -> None:
    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("my-run") as root:
            root.set_attributes({"agentflight.kind": "run", "agentflight.run.metadata": "{}"})
            with tracer.start_as_current_span("call") as s:
                s.set_attributes(
                    {
                        "agentflight.kind": "llm",
                        "agentflight.llm.model": "m1",
                        "agentflight.llm.tokens_in": 4,
                        "agentflight.llm.cost_usd": 0.25,
                        "gen_ai.request.model": "ignored",
                        "gen_ai.usage.input_tokens": 999,
                    }
                )

    spans = _record(build)
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert run["name"] == "my-run"
    assert (run["tokens_in"], run["cost_usd"]) == (4, 0.25)
    call = next(e for e in run["events"] if e["name"] == "call")
    assert call["attributes"]["agentflight.llm.model"] == "m1"
    assert call["attributes"]["gen_ai.usage.input_tokens"] == 999  # kept, not used


# Auth -------------------------------------------------------------------------------


def test_auth(engine: Engine) -> None:
    anon = TestClient(create_app(engine, _settings()))
    body = _protobuf(_record(_agent_trace))

    resp = anon.post("/v1/traces", content=body, headers=PB)
    assert resp.status_code == 401
    assert resp.headers["content-type"] == "application/x-protobuf"
    assert _status(resp)[0] == otlp.UNAUTHENTICATED
    assert resp.headers["www-authenticate"] == "Bearer"

    wrong = {**PB, "Authorization": "Bearer " + "x" * 40}
    assert anon.post("/v1/traces", content=body, headers=wrong).status_code == 401

    read_only = {**PB, "Authorization": f"Bearer {READ_KEY}"}
    resp = anon.post("/v1/traces", content=body, headers=read_only)
    assert resp.status_code == 403
    assert _status(resp)[0] == otlp.PERMISSION_DENIED

    resp = anon.post("/v1/traces", content=_json(_record(_agent_trace)), headers=JS)
    assert resp.status_code == 401
    assert resp.json()["code"] == otlp.UNAUTHENTICATED

    for headers in (
        {"Authorization": f"Bearer {INGEST_KEY}"},
        {"x-agentflight-key": INGEST_KEY},
        {"Authorization": f"Bearer {TEST_KEY}"},
    ):
        assert anon.post("/v1/traces", content=body, headers={**PB, **headers}).status_code == 200


def test_auth_disabled(engine: Engine) -> None:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)))
    body = _protobuf(_record(_agent_trace))
    assert c.post("/v1/traces", content=body, headers=PB).status_code == 200


# Unsupported, malformed and oversized requests ----------------------------------------


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/grpc", "application/xml"]
)
def test_unsupported_content_type(api: TestClient, content_type: str | None) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    resp = api.post("/v1/traces", content=b"{}", headers=headers)
    assert resp.status_code == 415
    assert resp.json()["code"] == otlp.INVALID_ARGUMENT


@pytest.mark.parametrize("encoding", ["br", "deflate", "zstd", "gzip, br"])
def test_unsupported_content_encoding(api: TestClient, encoding: str) -> None:
    headers = {**PB, "Content-Encoding": encoding}
    resp = api.post("/v1/traces", content=b"x", headers=headers)
    assert resp.status_code == 415
    assert _status(resp)[0] == otlp.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"\xff\xff\xff\xff", PB),  # not protobuf
        (b"\x0a\x05\x0a", PB),  # truncated message
        (b"{not json", JS),
        (b"[1, 2]", JS),
        (b'{"resourceSpans": 5}', JS),
        (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": 7}]}]}]}', JS),
        (b"\xff\xfe", JS),  # not UTF-8
        (b"not gzip at all", {**PB, "Content-Encoding": "gzip"}),
        (gzip.compress(b"\x0a" * 100)[:-6], {**PB, "Content-Encoding": "gzip"}),  # truncated
    ],
)
def test_malformed_body_is_400(api: TestClient, body: bytes, headers: dict[str, str]) -> None:
    resp = api.post("/v1/traces", content=body, headers=headers)
    assert resp.status_code == 400, resp.text
    code = _status(resp)[0] if headers is PB or "Content-Encoding" in headers else None
    if code is None:
        code = resp.json()["code"]
    assert code == otlp.INVALID_ARGUMENT


def test_empty_request_is_ok(api: TestClient, engine: Engine) -> None:
    for body, headers in [
        (b"", PB),
        (b"{}", JS),
        (gzip.compress(b""), {**PB, "Content-Encoding": "gzip"}),
    ]:
        resp = api.post("/v1/traces", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
    assert _counts(engine) == (0, 0)


def test_body_over_the_cap_is_413(engine: Engine) -> None:
    c = TestClient(
        create_app(engine, _settings(max_body_bytes=4096)),
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )
    big = b"\x00" * 5000
    resp = c.post("/v1/traces", content=big, headers=PB)  # Content-Length check
    assert resp.status_code == 413
    assert resp.headers["content-type"] == "application/x-protobuf"
    assert _status(resp)[0] == otlp.RESOURCE_EXHAUSTED

    def chunks() -> Iterator[bytes]:  # no Content-Length: the streaming count
        for _ in range(5):
            yield b"\x00" * 1024

    resp = c.post("/v1/traces", content=chunks(), headers=JS)
    assert resp.status_code == 413
    assert resp.json()["code"] == otlp.RESOURCE_EXHAUSTED


def test_gzip_bomb_is_413(engine: Engine) -> None:
    cap = 1024 * 1024
    c = TestClient(
        create_app(engine, _settings(max_body_bytes=cap)),
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )
    bomb = gzip.compress(b"\x00" * (64 * cap))  # 64 MiB that compresses to ~64 KiB
    assert len(bomb) < cap
    resp = c.post("/v1/traces", content=bomb, headers={**PB, "Content-Encoding": "gzip"})
    assert resp.status_code == 413
    code, message = _status(resp)
    assert code == otlp.RESOURCE_EXHAUSTED
    assert "decompressed" in message


def test_decompress_stops_at_the_cap() -> None:
    bomb = gzip.compress(b"a" * 10_000_000)
    with pytest.raises(otlp.OTLPError) as exc:
        otlp.decompress(bomb, "gzip", 1000)
    assert exc.value.status == 413
    # Exactly at the cap is fine; multi-member gzip streams are joined.
    assert otlp.decompress(gzip.compress(b"a" * 1000), "gzip", 1000) == b"a" * 1000
    two = gzip.compress(b"ab") + gzip.compress(b"cd")
    assert otlp.decompress(two, "gzip", 1000) == b"abcd"
    assert otlp.decompress(b"raw", "identity", 3) == b"raw"


def test_too_many_spans_is_413(engine: Engine) -> None:
    c = TestClient(
        create_app(engine, _settings(max_spans_per_batch=3)),
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )
    resp = c.post("/v1/traces", content=_protobuf(_record(_agent_trace)), headers=PB)
    assert resp.status_code == 413
    assert "4 spans" in _status(resp)[1]
    assert _counts(engine) == (0, 0)


def test_bad_spans_are_rejected_individually(engine: Engine) -> None:
    c = TestClient(
        create_app(engine, _settings(max_attribute_chars=100)),
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("agent"):
            with tracer.start_as_current_span("huge") as s:
                s.set_attribute("blob", "x" * 500)
            with tracer.start_as_current_span("fine"):
                pass

    spans = _record(build)
    request = _request(spans)
    bad = request.resource_spans[0].scope_spans[0].spans.add()
    bad.CopyFrom(request.resource_spans[0].scope_spans[0].spans[0])
    bad.name, bad.trace_id = "zero-trace", b"\x00" * 16
    short = request.resource_spans[0].scope_spans[0].spans.add()
    short.CopyFrom(bad)
    short.name, short.trace_id, short.span_id = "short-id", b"\x01" * 16, b"\x01" * 3

    resp = c.post("/v1/traces", content=request.SerializeToString(), headers=PB)
    assert resp.status_code == 200
    partial = _response(resp).partial_success
    assert partial.rejected_spans == 3
    assert "'huge'" in partial.error_message and "blob" in partial.error_message
    assert "'zero-trace': invalid trace_id" in partial.error_message
    assert "'short-id': invalid span_id" in partial.error_message
    run = c.get(f"/v1/runs/{_run_id(spans)}").json()
    assert sorted(e["name"] for e in run["events"]) == ["agent", "fine"]

    resp = c.post("/v1/traces", content=_json(spans), headers=JS)
    assert resp.json()["partialSuccess"]["rejectedSpans"] == "1"


def test_partial_success_message_is_bounded() -> None:
    converted = otlp.Converted()
    for i in range(12):
        converted.reject(f"span {i}: bad")
    message = converted.error_message
    assert converted.rejected == 12
    assert message.count("bad") == 5 and message.endswith("and 7 more")


def test_database_errors_are_retryable_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*_: Any, **__: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("connection refused"))

    monkeypatch.setattr(service, "ingest_spans", down)
    resp = api.post("/v1/traces", content=_protobuf(_record(_agent_trace)), headers=PB)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    code, message = _status(resp)
    assert code == otlp.UNAVAILABLE
    assert "connection refused" not in message


# Viewer -----------------------------------------------------------------------------


def test_viewer_shows_the_span_tree(engine: Engine) -> None:
    c = TestClient(create_app(engine, Settings(auth_disabled=True)))
    spans = _record(_agent_trace)
    assert c.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    page = c.get(f"/ui/runs/{_run_id(spans)}")
    assert page.status_code == 200
    for name in ["support-agent", "chat gpt-4o", "execute_tool lookup_order", "chat gpt-4o-mini"]:
        assert name in page.text
    assert "lookup_order" in c.get("/ui/runs").text or "support-agent" in c.get("/ui/runs").text
    llm = next(s for s in spans if s.name == "chat gpt-4o")
    assert llm.context is not None
    detail = c.get(f"/ui/runs/{_run_id(spans)}/events/{llm.context.span_id:016x}")
    assert detail.status_code == 200
    assert "gen_ai.usage.input_tokens" in detail.text


# A real OpenTelemetry exporter, over HTTP --------------------------------------------


@pytest.fixture
def live_server(engine: Engine) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(engine, _settings())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "test server did not start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.parametrize("compression", [Compression.NoCompression, Compression.Gzip])
def test_real_otlp_exporter_end_to_end(
    live_server: str, engine: Engine, compression: Compression
) -> None:
    exporter = OTLPSpanExporter(
        endpoint=f"{live_server}/v1/traces",
        headers={"Authorization": f"Bearer {INGEST_KEY}"},
        compression=compression,
        timeout=10,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": "e2e-agent"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("e2e")
    with tracer.start_as_current_span("e2e-run") as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        with tracer.start_as_current_span("chat") as llm:
            llm.set_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-4o",
                    "gen_ai.usage.input_tokens": 11,
                    "gen_ai.usage.output_tokens": 2,
                }
            )
    provider.shutdown()

    reader = TestClient(create_app(engine, _settings()))
    run = reader.get(f"/v1/runs/{trace_id}", headers={"Authorization": f"Bearer {READ_KEY}"})
    assert run.status_code == 200, run.text
    body = run.json()
    assert (body["name"], body["status"], body["event_count"]) == ("e2e-run", "ok", 2)
    assert (body["llm_calls"], body["tokens_in"], body["tokens_out"]) == (1, 11, 2)
    assert body["metadata"] == {"service.name": "e2e-agent"}


def test_real_otlp_exporter_reports_auth_failure(live_server: str) -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    exporter = OTLPSpanExporter(
        endpoint=f"{live_server}/v1/traces",
        headers={"Authorization": f"Bearer {READ_KEY}"},
        timeout=5,
    )
    spans = _record(_agent_trace)
    assert exporter.export(spans) == SpanExportResult.FAILURE
    exporter.shutdown()


def test_agentflight_sdk_over_otlp_keeps_run_semantics(live_server: str, engine: Engine) -> None:
    """The AgentFlight SDK can export through a stock OTLP exporter (e.g. via a Collector)."""
    import agentflight
    from agentflight import attributes as A
    from agentflight.replayer import Recording

    exporter = OTLPSpanExporter(
        endpoint=f"{live_server}/v1/traces",
        headers={"x-agentflight-key": INGEST_KEY},
        timeout=10,
    )
    agentflight.configure(exporter=exporter, batch=False)
    try:
        messages = [{"role": "user", "content": "hi"}]
        with (
            agentflight.run("sdk-run", metadata={"user": "u1"}) as r,
            agentflight.llm_call("toy-1", messages) as call,
        ):
            call.record("hello", tokens_in=3, tokens_out=1, cost_usd=0.5)
        run_id = r.run_id
    finally:
        agentflight.shutdown()

    reader = TestClient(create_app(engine, _settings()))
    body = reader.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {READ_KEY}"}).json()
    assert (body["name"], body["status"], body["metadata"]) == ("sdk-run", "ok", {"user": "u1"})
    assert (body["tokens_in"], body["tokens_out"], body["cost_usd"]) == (3, 1, 0.5)
    kinds = sorted(e["kind"] for e in body["events"])
    assert kinds == [A.LLM, A.RUN]
    # SDK-recorded outputs travel through OTLP unchanged, so the run stays replayable.
    steps = Recording.from_run(body).steps
    assert len(steps) == 1


def test_names_over_255_chars_are_rejected_not_truncated(api: TestClient) -> None:
    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("agent"):
            with tracer.start_as_current_span("n" * 256):
                pass
            with tracer.start_as_current_span("event-name") as s:
                s.add_event("e" * 256)
            with tracer.start_as_current_span("x" * 255):  # exactly at the limit
                pass

    spans = _record(build)
    resp = api.post("/v1/traces", content=_protobuf(spans), headers=PB)
    assert resp.status_code == 200
    partial = _response(resp).partial_success
    assert partial.rejected_spans == 2
    message = partial.error_message
    assert ": name: String should have at most 255 characters" in message
    assert "'event-name': event name: String should have at most 255 characters" in message
    run = api.get(f"/v1/runs/{_run_id(spans)}").json()
    assert sorted(len(e["name"]) for e in run["events"]) == [5, 255]
