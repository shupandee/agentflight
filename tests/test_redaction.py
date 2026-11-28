"""Server-side redaction (AGENTFLIGHT_REDACTION_FILE): the engine, both ingest routes,
replay of redacted recordings, and a canary secret that must not survive anywhere."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode
from sqlalchemy import Engine, select

import agentflight
from agentflight import attributes as A
from agentflight import redaction
from agentflight.redaction import FAILED, REDACTED, Policy, PolicyError
from agentflight.replayer import Recording, RedactedInputError, ReplayError
from agentflight.server import create_app
from agentflight.server.models import Base
from agentflight.server.settings import Settings
from tests.conftest import TEST_KEY

CANARY = "sk-canary0123456789abcdefCANARY"
POLICY = """
version = 1

[[rules]]
name = "keys"
keys = ["api_key", "password", "Authorization"]

[[rules]]
name = "openai"
preset = "openai-key"

[[rules]]
name = "bearer"
preset = ["bearer"]

[[rules]]
name = "aws"
preset = "aws-access-key-id"

[[rules]]
name = "internal"
patterns = ['secret-[a-z]+']

[[rules]]
name = "ssn"
attributes = ["customer.ssn"]
action = "drop"
"""
T = "a" * 32


def _policy(text: str = POLICY, on_error: str = "redact") -> Policy:
    import tomllib

    return Policy.from_dict(tomllib.loads(text), on_error=on_error)


def _app(engine: Engine, tmp_path: Path, policy: str = POLICY, **settings: Any) -> TestClient:
    path = tmp_path / "redaction.toml"
    path.write_text(policy)
    app = create_app(engine, Settings(api_key=TEST_KEY, redaction_file=str(path), **settings))
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


@pytest.fixture
def api(engine: Engine, tmp_path: Path) -> Iterator[TestClient]:
    with _app(engine, tmp_path) as c:
        yield c


@pytest.fixture
def rec(api: TestClient) -> Iterator[TestClient]:
    """The SDK recording into a server with the redaction policy."""
    agentflight.configure(
        exporter=agentflight.AgentFlightExporter("http://testserver", client=api), batch=False
    )
    yield api
    agentflight.shutdown()


def span(sid: str, attrs: dict[str, Any], **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "trace_id": T,
        "span_id": sid.rjust(16, "0"),
        "name": kw.pop("name", "step"),
        "start_time_ns": 1_000 + int(sid, 16),
        "end_time_ns": 2_000 + int(sid, 16),
        "attributes": attrs,
    }
    out.update(kw)
    return out


def events(client: TestClient, run_id: str = T) -> list[dict[str, Any]]:
    resp = client.get(f"/v1/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["events"])


def event(client: TestClient, sid: str, run_id: str = T) -> dict[str, Any]:
    return next(e for e in events(client, run_id) if e["span_id"] == sid.rjust(16, "0"))


def db_text(engine: Engine) -> str:
    """Every stored row of every table, as text."""
    out: list[str] = []
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            out.extend(repr(tuple(row)) for row in conn.execute(select(table)))
    return "\n".join(out)


# -- policy loading and validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ('name = "x"\npreset = "nope"', "unknown preset"),
        ('name = "x"\npatterns = ["("]', "invalid pattern"),
        ('name = "x"\npatterns = ["(a+)+b"]', "nested quantifier"),
        ('name = "x"\nattributes = ["agentflight.tool.name"]', "structural"),
        ('name = "x"\nattributes = ["agentflight.llm.model"]', "structural"),
        ('name = "x"\nattributes = ["agentflight.replay.mode"]', "structural"),
        ('name = "x"\npatterns = ["REDACTED"]', "matches the marker"),
        ('name = "x"\npatterns = ["FAILED"]', "matches the marker"),
        ('name = "x"\npatterns = ["a"]\naction = "drop"', "not patterns"),
        ('name = "x"\nkeys = ["a"]\nscope = ["body"]', "unknown scope"),
        ('name = "x"', "needs keys"),
        ('name = "x y"\nkeys = ["a"]', "name must be"),
        ('name = "x"\nkeys = ["a"]\nextra = 1', "unknown settings"),
    ],
)
def test_invalid_policies_are_refused(rules: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        _policy(f"[[rules]]\n{rules}\n")


def test_policy_level_validation() -> None:
    with pytest.raises(PolicyError, match="at least one"):
        _policy("version = 1\n")
    with pytest.raises(PolicyError, match="version"):
        _policy('version = 2\n[[rules]]\nname = "x"\nkeys = ["a"]\n')
    with pytest.raises(PolicyError, match="unique"):
        _policy('[[rules]]\nname = "x"\nkeys = ["a"]\n[[rules]]\nname = "x"\nkeys = ["b"]\n')
    with pytest.raises(PolicyError, match="on_error"):
        _policy(on_error="ignore")


def test_server_refuses_to_start_with_a_bad_policy(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"AGENTFLIGHT_REDACTION_FILE.*unknown preset"):
        _app(engine, tmp_path, '[[rules]]\nname = "x"\npreset = "nope"\n')
    with pytest.raises(RuntimeError, match="isn't valid TOML"):
        _app(engine, tmp_path, "[[rules]\n")
    with pytest.raises(RuntimeError, match="can't read"):
        create_app(
            engine, Settings(api_key=TEST_KEY, redaction_file=str(tmp_path / "missing.toml"))
        )
    with pytest.raises(RuntimeError, match="REDACTION_ON_ERROR"):
        _app(engine, tmp_path, redaction_on_error="ignore")


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFLIGHT_REDACTION_FILE", "/etc/agentflight/redaction.toml")
    monkeypatch.setenv("AGENTFLIGHT_REDACTION_ON_ERROR", " Reject ")
    s = Settings.from_env()
    assert (s.redaction_file, s.redaction_on_error) == ("/etc/agentflight/redaction.toml", "reject")
    monkeypatch.delenv("AGENTFLIGHT_REDACTION_FILE")
    monkeypatch.delenv("AGENTFLIGHT_REDACTION_ON_ERROR")
    s = Settings.from_env()
    assert (s.redaction_file, s.redaction_on_error) == (None, "redact")


# -- the engine ---------------------------------------------------------------------------


def redact(policy: Policy, attrs: dict[str, Any], **kw: Any) -> redaction.SpanResult:
    return redaction.redact_span_parts(
        policy, attrs, kw.pop("events", []), kw.pop("status", None), max_chars=kw.pop("cap", None)
    )


def test_presets_match_key_shaped_secrets() -> None:
    policy = _policy()
    text = (
        "Authorization: Bearer abcdefghijklmnop0123456789== key AKIAABCDEFGHIJKLMNOP "
        f"temp ASIAABCDEFGHIJKLMNOP and {CANARY} and sk-proj-{'x' * 24} ok"
    )
    out = redact(policy, {"note": text}).attributes["note"]
    assert out == (
        "Authorization: [REDACTED:bearer] key [REDACTED:aws] temp [REDACTED:aws] and "
        "[REDACTED:openai] and [REDACTED:openai] ok"
    )
    # Short look-alikes aren't secrets.
    assert redact(policy, {"note": "sk-short bearer x AKIA123"}).attributes["note"] == (
        "sk-short bearer x AKIA123"
    )


def test_key_rules_walk_json_payloads_at_any_depth_case_insensitively() -> None:
    payload = json.dumps(
        {"API_KEY": "k1", "nested": [{"Password": 5}, {"keep": "secret-abc"}], "ok": 1}
    )
    result = redact(_policy(), {"agentflight.tool.args": payload})
    assert json.loads(result.attributes["agentflight.tool.args"]) == {
        "API_KEY": "[REDACTED:keys]",
        "nested": [{"Password": "[REDACTED:keys]"}, {"keep": "[REDACTED:internal]"}],
        "ok": 1,
    }
    report = json.loads(result.attributes[REDACTED])
    assert report == {"agentflight.tool.args": {"rules": ["keys", "internal"], "count": 3}}


def test_unchanged_values_keep_their_bytes_and_no_report() -> None:
    payload = '{"b": 1,   "a": "x"}'
    result = redact(_policy(), {"agentflight.tool.args": payload, "n": 3, "flag": True})
    assert result.attributes == {"agentflight.tool.args": payload, "n": 3, "flag": True}


def test_drop_removes_the_attribute_or_entry() -> None:
    policy = _policy(POLICY + '\n[[rules]]\nname = "gone"\nkeys = ["token"]\naction = "drop"\n')
    result = redact(
        policy, {"customer.ssn": "123-45-6789", "agentflight.tool.args": '{"token": "t", "a": 1}'}
    )
    assert "customer.ssn" not in result.attributes
    assert json.loads(result.attributes["agentflight.tool.args"]) == {"a": 1}
    report = json.loads(result.attributes[REDACTED])
    assert report["customer.ssn"] == {"rules": ["ssn"], "count": 1, "dropped": True}


def test_scope_limits_where_a_rule_applies() -> None:
    policy = _policy('[[rules]]\nname = "s"\npatterns = ["hush"]\nscope = ["status"]\n')
    result = redact(
        policy,
        {"note": "hush"},
        events=[("log", 1, {"msg": "hush"})],
        status="Error: hush",
    )
    assert result.attributes["note"] == "hush"
    assert result.events[0]["attributes"]["msg"] == "hush"
    assert result.status_message == "Error: [REDACTED:s]"


def test_custom_marker() -> None:
    policy = _policy('marker = "***{rule}***"\n[[rules]]\nname = "s"\npatterns = ["hush"]\n')
    assert redact(policy, {"note": "a hush"}).attributes["note"] == "a ***s***"


def test_redacting_twice_changes_nothing() -> None:
    policy = _policy()
    attrs = {
        "agentflight.tool.args": json.dumps({"api_key": CANARY, "q": f"Bearer {'t' * 20}"}),
        "note": f"use {CANARY}",
    }
    once = redact(policy, attrs, status=f"failed with {CANARY}")
    twice = redact(policy, once.attributes, status=once.status_message)
    without_report = {k: v for k, v in once.attributes.items() if k != REDACTED}
    assert {k: v for k, v in twice.attributes.items() if k != REDACTED} == without_report
    assert twice.status_message == once.status_message
    assert REDACTED not in twice.attributes  # nothing new was redacted


# Change 1: truncated payloads ------------------------------------------------------------


def test_truncated_payloads_use_text_matching_not_failed() -> None:
    cut = '{"messages": [{"api_key": "abc123", "n": 4, "password": null, "t": "' + CANARY
    attrs = {
        "agentflight.llm.messages": cut,
        A.TRUNCATED: json.dumps({"agentflight.llm.messages": {"original_length": 9999}}),
    }
    result = redact(_policy(), attrs)
    out = result.attributes["agentflight.llm.messages"]
    assert FAILED not in out
    assert out == (
        '{"messages": [{"api_key": "[REDACTED:keys]", "n": 4, "password": "[REDACTED:keys]",'
        ' "t": "[REDACTED:openai]'
    )
    assert result.attributes[A.TRUNCATED] == attrs[A.TRUNCATED]  # structural, untouched


def test_text_matching_handles_a_value_cut_off_mid_string() -> None:
    attrs = {
        "agentflight.tool.args": '{"q": 1, "api_key": "abc12',
        A.TRUNCATED: json.dumps({"agentflight.tool.args": {}}),
    }
    out = redact(_policy(), attrs).attributes["agentflight.tool.args"]
    assert out == '{"q": 1, "api_key": "[REDACTED:keys]"'


def test_a_secret_cut_by_truncation_may_not_match() -> None:
    # Documented limit: the cut leaves "sk-canary01" (too short for the preset).
    attrs = {
        "agentflight.tool.args": '{"q": "sk-canary01',
        A.TRUNCATED: '{"agentflight.tool.args": {}}',
    }
    assert redact(_policy(), attrs).attributes["agentflight.tool.args"] == '{"q": "sk-canary01'


def test_unparsable_json_payloads_also_use_text_matching() -> None:
    attrs = {"agentflight.tool.result": '{"api_key": "abc", broken'}
    out = redact(_policy(), attrs).attributes["agentflight.tool.result"]
    assert out == '{"api_key": "[REDACTED:keys]", broken'


def test_truncated_payloads_recorded_by_the_sdk(engine: Engine, tmp_path: Path) -> None:
    with _app(engine, tmp_path) as api:
        agentflight.configure(
            exporter=agentflight.AgentFlightExporter("http://testserver", client=api),
            batch=False,
            max_attr_chars=200,
        )
        try:

            @agentflight.tool
            def call_api(config: dict[str, Any]) -> str:
                return "ok"

            with agentflight.run("trunc") as r:
                call_api({"api_key": "abc123", "padding": "x" * 500})
            agentflight.flush()
        finally:
            agentflight.shutdown()
        tool = next(e for e in events(api, r.run_id) if e["kind"] == "tool")
    args = tool["attributes"]["agentflight.tool.args"]
    assert "agentflight.tool.args" in tool["truncated"]
    assert '"api_key": "[REDACTED:keys]"' in args and "abc123" not in args
    assert tool["redacted"]["agentflight.tool.args"]["rules"] == ["keys"]


# Change 2: JSON carried in plain string attributes ----------------------------------------


def test_key_rules_apply_to_json_in_plain_string_attributes() -> None:
    messages = json.dumps([{"role": "user", "content": "hi", "api_key": "k"}])
    config = json.dumps({"db": {"password": "p"}})
    result = redact(
        _policy(),
        {
            "gen_ai.input.messages": messages,
            "app.config": "  " + config,
            "app.note": "{not json api_key: x}",
            "app.number": "42",
        },
    )
    a = result.attributes
    assert json.loads(a["gen_ai.input.messages"])[0]["api_key"] == "[REDACTED:keys]"
    assert json.loads(a["app.config"]) == {"db": {"password": "[REDACTED:keys]"}}
    assert a["app.note"] == "{not json api_key: x}"  # text mode: no "key": value pair
    assert a["app.number"] == "42"


def test_json_inside_json_strings_is_walked() -> None:
    inner = json.dumps({"password": "p"})
    outer = json.dumps({"tool_call": {"arguments": inner}})
    result = redact(_policy(), {"agentflight.llm.response": outer})
    args = json.loads(
        json.loads(result.attributes["agentflight.llm.response"])["tool_call"]["arguments"]
    )
    assert args == {"password": "[REDACTED:keys]"}


def test_structured_values_lists_and_maps_are_walked() -> None:
    result = redact(
        _policy(),
        {
            "otel.links": [{"trace_id": "t", "attributes": {"api_key": "k", "x": "secret-a"}}],
            "tags": ["fine", "secret-b"],
        },
    )
    assert result.attributes["otel.links"] == [
        {"trace_id": "t", "attributes": {"api_key": "[REDACTED:keys]", "x": "[REDACTED:internal]"}}
    ]
    assert result.attributes["tags"] == ["fine", "[REDACTED:internal]"]


def test_otlp_gen_ai_json_strings_are_redacted(api: TestClient) -> None:
    from tests.test_otlp import PB, _protobuf

    def build(tracer: Any) -> None:
        with tracer.start_as_current_span("chat gpt-4o") as s:
            s.set_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-4o",
                    "gen_ai.input.messages": json.dumps(
                        [{"role": "user", "parts": [{"content": "hi", "api_key": "k"}]}]
                    ),
                }
            )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    build(provider.get_tracer("t"))
    spans = list(exporter.get_finished_spans())
    assert api.post("/v1/traces", content=_protobuf(spans), headers=PB).status_code == 200
    run_id = f"{spans[0].context.trace_id:032x}"
    stored = events(api, run_id)[0]["attributes"]
    assert stored["gen_ai.input.messages"] == [
        {"role": "user", "parts": [{"content": "hi", "api_key": "[REDACTED:keys]"}]}
    ]
    assert '"k"' not in json.dumps(stored)


# Change 4: never-redacted names -----------------------------------------------------------


def test_names_and_structural_attributes_are_never_redacted(api: TestClient) -> None:
    policy = POLICY + '\n[[rules]]\nname = "all"\nattributes = ["agentflight.*"]\n'
    attrs = {
        A.KIND: "tool",
        A.TOOL_NAME: "secret-tool",
        A.LLM_MODEL: "secret-model",
        A.RUN_NAME: "secret-run",
        A.TOOL_ARGS: '{"q": 1}',
        A.REPLAY_MODE: "replay",
    }
    result = redact(_policy(policy), attrs)
    a = result.attributes
    for key in (A.KIND, A.TOOL_NAME, A.LLM_MODEL, A.RUN_NAME, A.REPLAY_MODE):
        assert a[key] == attrs[key], key
    assert a[A.TOOL_ARGS] == "[REDACTED:all]"  # the glob still covers payloads


def test_tool_and_model_names_survive_ingest(api: TestClient) -> None:
    resp = api.post(
        "/v1/spans",
        json={
            "spans": [
                span("1", {A.KIND: "run", A.RUN_NAME: "secret-run"}, name="secret-run"),
                span(
                    "2",
                    {A.KIND: "tool", A.TOOL_NAME: "secret-tool", A.TOOL_ARGS: '{"x": "secret-x"}'},
                    parent_span_id="1".rjust(16, "0"),
                    name="secret-tool",
                ),
                span(
                    "3",
                    {A.KIND: "llm", A.LLM_MODEL: "secret-model"},
                    parent_span_id="1".rjust(16, "0"),
                ),
            ]
        },
    )
    assert resp.status_code == 202, resp.text
    assert api.get(f"/v1/runs/{T}").json()["name"] == "secret-run"
    tool = event(api, "2")
    assert (tool["name"], tool["attributes"][A.TOOL_NAME]) == ("secret-tool", "secret-tool")
    assert tool["attributes"][A.TOOL_ARGS] == {"x": "[REDACTED:internal]"}
    assert event(api, "3")["attributes"][A.LLM_MODEL] == "secret-model"


# Change 5: reject mode on /v1/spans and OTLP ---------------------------------------------


def _break_one_value(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    real = Policy.redact_value

    def flaky(self: Policy, k: str, value: Any, *a: Any, **kw: Any) -> Any:
        if k == key:
            raise RuntimeError(f"boom {value}")
        return real(self, k, value, *a, **kw)

    monkeypatch.setattr(Policy, "redact_value", flaky)


def test_a_failing_value_is_stored_as_failed_by_default(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_one_value(monkeypatch, "app.blob")
    resp = api.post(
        "/v1/spans", json={"spans": [span("1", {"app.blob": CANARY, "app.ok": "fine"})]}
    )
    assert resp.json() == {"accepted": 1, "runs": [T], "rejected": 0, "error_message": None}
    e = event(api, "1")
    assert e["attributes"]["app.blob"] == FAILED
    assert e["attributes"]["app.ok"] == "fine"
    assert e["redacted"]["app.blob"] == {"rules": ["<error>"], "count": 1}
    assert CANARY not in db_text(engine)


def test_reject_mode_stores_the_other_spans_and_counts_rejections(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_one_value(monkeypatch, "app.blob")
    with _app(engine, tmp_path, redaction_on_error="reject") as api:
        resp = api.post(
            "/v1/spans",
            json={
                "spans": [
                    span("1", {A.KIND: "run", A.RUN_NAME: "r"}),
                    span("2", {"app.blob": CANARY}, parent_span_id="1".rjust(16, "0")),
                    span("3", {"app.ok": "x"}, parent_span_id="1".rjust(16, "0")),
                ]
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert (body["accepted"], body["rejected"], body["runs"]) == (2, 1, [T])
        assert body["error_message"] == f"span {'2'.rjust(16, '0')}: redaction failed for app.blob"
        assert CANARY not in resp.text
        assert {e["span_id"][-1] for e in events(api)} == {"1", "3"}
        # A batch with only rejected spans stores nothing and is still a 202.
        only = api.post("/v1/spans", json={"spans": [span("4", {"app.blob": "y"})]})
        assert (only.status_code, only.json()["accepted"], only.json()["rejected"]) == (202, 0, 1)
    assert CANARY not in db_text(engine)


def test_reject_mode_on_otlp_reports_partial_success(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_otlp import PB, _protobuf

    _break_one_value(monkeypatch, "app.blob")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("root"), tracer.start_as_current_span("bad") as bad:
        bad.set_attribute("app.blob", CANARY)
    spans = list(exporter.get_finished_spans())
    with _app(engine, tmp_path, redaction_on_error="reject") as api:
        resp = api.post("/v1/traces", content=_protobuf(spans), headers=PB)
        assert resp.status_code == 200
        out = ExportTraceServiceResponse()
        out.ParseFromString(resp.content)
        assert out.partial_success.rejected_spans == 1
        assert "redaction failed for app.blob" in out.partial_success.error_message
        assert CANARY not in out.partial_success.error_message
        run_id = f"{spans[0].context.trace_id:032x}"
        assert [e["name"] for e in events(api, run_id)] == ["root"]


# Smaller changes: marker over the cap, re-sends overwrite the report ----------------------


def test_marker_longer_than_the_cap_replaces_the_whole_value() -> None:
    policy = _policy(
        'marker = "[REDACTED-BY-POLICY:{rule}]"\n[[rules]]\nname = "d"\npatterns = ["\\\\d"]\n'
    )
    result = redact(policy, {"note": "1234567890"}, cap=40)
    assert result.attributes["note"] == "[REDACTED-BY-POLICY:d]"
    report = json.loads(result.attributes[REDACTED])
    assert report["note"] == {"rules": ["d"], "count": 10, "over_cap": True}
    under = redact(policy, {"note": "12"}, cap=60).attributes["note"]
    assert under == "[REDACTED-BY-POLICY:d][REDACTED-BY-POLICY:d]"


def test_marker_cap_on_ingest_uses_the_server_attribute_limit(
    engine: Engine, tmp_path: Path
) -> None:
    with _app(engine, tmp_path, max_attribute_chars=100) as api:
        resp = api.post("/v1/spans", json={"spans": [span("1", {"note": "secret-a " * 11})]})
        assert resp.status_code == 202
        e = event(api, "1")
    assert e["attributes"]["note"] == "[REDACTED:internal]"
    assert e["redacted"]["note"]["over_cap"] is True


def test_a_re_send_overwrites_the_report(api: TestClient) -> None:
    forged = json.dumps({"app.other": {"rules": ["nope"], "count": 9}})
    api.post("/v1/spans", json={"spans": [span("1", {"a": "secret-a", REDACTED: forged})]})
    assert event(api, "1")["redacted"] == {"a": {"rules": ["internal"], "count": 1}}
    api.post("/v1/spans", json={"spans": [span("1", {"b": "secret-b"})]})
    assert event(api, "1")["redacted"] == {"b": {"rules": ["internal"], "count": 1}}
    api.post("/v1/spans", json={"spans": [span("1", {"c": "clean", REDACTED: forged})]})
    e = event(api, "1")
    assert e["redacted"] == {} and REDACTED not in e["attributes"]


def test_without_a_policy_spans_are_stored_as_sent(client: TestClient) -> None:
    attrs = {"note": CANARY, REDACTED: '{"x": {}}'}
    client.post("/v1/spans", json={"spans": [span("1", attrs)]})
    e = event(client, "1")
    assert e["attributes"]["note"] == CANARY


def test_payloads_false_keeps_the_report(api: TestClient) -> None:
    api.post("/v1/spans", json={"spans": [span("1", {A.TOOL_ARGS: '{"api_key": "k"}'})]})
    run = api.get(f"/v1/runs/{T}", params={"payloads": "false"}).json()
    e = run["events"][0]
    assert A.TOOL_ARGS in e["omitted"] and REDACTED not in e["omitted"]
    assert e["redacted"][A.TOOL_ARGS]["rules"] == ["keys"]


# Change 3: replay of redacted recordings ----------------------------------------------------


@agentflight.tool
def lookup(api_key: str, q: str) -> str:
    return f"result {q}"


def _recorded_lookup(client: TestClient) -> Recording:
    with agentflight.run("lookup") as r:
        lookup("abc-key", "delhi")
        lookup("none", "paris")
    agentflight.flush()
    return Recording.fetch(r.run_id, "http://testserver", api_key=TEST_KEY, client=client)


def test_a_redacted_input_raises_even_with_on_divergence_live(rec: TestClient) -> None:
    recording = _recorded_lookup(rec)
    assert A.TOOL_ARGS in recording.steps[0].redacted
    with (
        agentflight.replay(recording),
        pytest.raises(RedactedInputError, match=r"step 0 .*redacted by the server"),
    ):
        lookup("abc-key", "delhi")
    with (
        agentflight.replay(recording, on_divergence="live"),
        pytest.raises(RedactedInputError) as caught,
    ):
        lookup("abc-key", "delhi")
    assert "abc-key" not in str(caught.value)


def test_on_redacted_input_live_runs_live_and_is_recorded(rec: TestClient) -> None:
    recording = _recorded_lookup(rec)
    with (
        agentflight.replay(recording, on_redacted_input="live") as session,
        agentflight.run("again") as r,
    ):
        assert lookup("abc-key", "delhi") == "result delhi"
    assert session.diverged_at == 0
    agentflight.flush()
    root = next(e for e in events(rec, r.run_id) if e["kind"] == "run")
    assert root["attributes"][A.REPLAY_ON_REDACTED_INPUT] == "live"
    assert root["attributes"][A.REPLAY_DIVERGED_AT] == 0
    # Not recorded when the opt-in isn't used.
    with agentflight.replay(recording), agentflight.run("plain") as plain:
        pass
    agentflight.flush()
    root = next(e for e in events(rec, plain.run_id) if e["kind"] == "run")
    assert A.REPLAY_ON_REDACTED_INPUT not in root["attributes"]
    with (
        pytest.raises(ValueError, match="on_redacted_input"),
        agentflight.replay(recording, on_redacted_input="skip"),  # type: ignore[arg-type]
    ):
        pass


def test_fork_supplies_a_redacted_output(rec: TestClient) -> None:
    @agentflight.tool
    def issue_token(user: str) -> str:
        return f"token {CANARY}"

    with agentflight.run("tokens") as r:
        issue_token("ana")
    agentflight.flush()
    recording = Recording.fetch(r.run_id, "http://testserver", api_key=TEST_KEY, client=rec)
    assert recording.steps[0].redacted[A.TOOL_RESULT]["rules"] == ["openai"]
    with (
        agentflight.replay(recording),
        pytest.raises(ReplayError, match=r"redacted by the server; fork\(recording, at=0"),
    ):
        issue_token("ana")
    with agentflight.fork(recording, 0, output="token placeholder"):
        assert issue_token("ana") == "token placeholder"


# Change 6: the canary --------------------------------------------------------------------


def _otlp_canary_spans() -> list[Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"deploy.secret": CANARY}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("otlp-root") as root:
        root.set_attribute("gen_ai.input.messages", json.dumps([{"api_key": "plainkey"}]))
        linked = root.get_span_context()
        with tracer.start_as_current_span(
            "child", links=[Link(linked, {"link.token": f"Bearer {CANARY}"})]
        ) as child:
            child.add_event("log", {"line": f"using {CANARY}"})
            child.set_status(Status(StatusCode.ERROR, f"auth failed for {CANARY}"))
    provider.shutdown()
    return list(exporter.get_finished_spans())


def test_canary_secret_is_stored_nowhere(
    rec: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.test_otlp import PB, _protobuf

    caplog.set_level(logging.DEBUG)

    @agentflight.tool
    def deploy(config: dict[str, Any], note: str) -> dict[str, Any]:
        if config.get("fail"):
            raise PermissionError(f"rejected key {CANARY}")
        return {"echo": CANARY, "password": "hunter2"}

    with agentflight.run("canary", metadata={"api_key": "metakey", "who": CANARY}) as r:
        deploy({"api_key": "abc"}, note=f"Authorization: Bearer {CANARY}")
        deploy({"blob": json.dumps({"password": "deep"})}, note=CANARY)
        try:
            deploy({"fail": True}, note="x")
        except PermissionError as exc:
            agentflight.error(exc)
        agentflight.state_change("creds", old=None, new={"token_hint": CANARY})
        with agentflight.llm_call("model-x", [{"role": "user", "content": CANARY}]) as call:
            call.record(f"echo {CANARY}")
    agentflight.flush()
    otlp_spans = _otlp_canary_spans()
    assert rec.post("/v1/traces", content=_protobuf(otlp_spans), headers=PB).status_code == 200
    otlp_run = f"{otlp_spans[0].context.trace_id:032x}"

    stored = db_text(engine)
    for secret in (CANARY, "metakey", "hunter2", "deep", "plainkey", '"abc"'):
        assert secret not in stored, secret
    pages = ["/ui"]
    for run_id in (r.run_id, otlp_run):
        assert CANARY not in rec.get(f"/v1/runs/{run_id}").text
        pages += [f"/ui/runs/{run_id}", f"/ui/runs/{run_id}/events"]
        for e in events(rec, run_id):
            assert CANARY not in rec.get(f"/v1/runs/{run_id}/events/{e['span_id']}").text
            pages.append(f"/ui/runs/{run_id}/events/{e['span_id']}")
    assert CANARY not in rec.get("/v1/runs").text
    with TestClient(rec.app, follow_redirects=False) as ui:
        login = ui.post(
            "/ui/login", data={"key": TEST_KEY}, headers={"Origin": "http://testserver"}
        )
        assert login.status_code == 303
        for page in pages:
            html = ui.get(page)
            assert html.status_code == 200, page
            assert CANARY not in html.text, page
    assert CANARY not in caplog.text
    # The redacted shapes are still recognisable.
    evs = events(rec, r.run_id)
    run = rec.get(f"/v1/runs/{r.run_id}").json()
    assert run["metadata"] == {"api_key": "[REDACTED:keys]", "who": "[REDACTED:openai]"}
    failed = next(e for e in evs if e["status"] == "error")
    assert failed["status_message"] == "PermissionError: rejected key [REDACTED:openai]"
    otlp_child = next(e for e in events(rec, otlp_run) if e["name"] == "child")
    assert otlp_child["status_message"] == "auth failed for [REDACTED:openai]"
    assert otlp_child["redacted"]["status_message"]["rules"] == ["openai"]


def test_canary_is_not_echoed_by_error_responses(api: TestClient) -> None:
    bad = [
        {"spans": [{**span("1", {"k": CANARY}), "trace_id": CANARY}]},
        {"spans": [{**span("1", {"k": CANARY}), "start_time_ns": CANARY}]},
        {"spans": [{**span("1", {}), "name": CANARY * 20}]},
        {"spans": CANARY},
    ]
    for body in bad:
        resp = api.post("/v1/spans", json=body)
        assert resp.status_code == 422
        assert CANARY not in resp.text
        assert resp.json()["detail"][0].keys() == {"loc", "msg", "type"}
    resp = api.post(
        "/v1/spans", content=f'{{"spans": [{CANARY}', headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 422 and CANARY not in resp.text


def test_canary_crash_path_replaces_everything_including_status(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*a: Any, **kw: Any) -> Any:
        raise MemoryError("engine crashed")

    monkeypatch.setattr(redaction, "_redact_span", crash)
    resp = api.post(
        "/v1/spans",
        json={
            "spans": [
                span(
                    "1",
                    {
                        A.KIND: "tool",
                        A.TOOL_NAME: "deploy",
                        A.TOOL_ARGS: json.dumps({"x": CANARY}),
                        "count": 3,
                        "list": [CANARY],
                        REDACTED: '{"forged": {}}',
                    },
                    status="error",
                    status_message=f"boom {CANARY}",
                    events=[{"name": "log", "time_ns": 5, "attributes": {"m": CANARY, "n": 1}}],
                )
            ]
        },
    )
    assert resp.status_code == 202
    e = event(api, "1")
    a = e["attributes"]
    assert (a[A.TOOL_ARGS], a["list"], a["count"], a[A.TOOL_NAME]) == (FAILED, FAILED, 3, "deploy")
    assert e["status_message"] == FAILED
    assert e["span_events"][0]["attributes"] == {"m": FAILED, "n": 1}
    assert set(e["redacted"]) == {A.TOOL_ARGS, "list", "status_message", "events[0].m"}
    assert CANARY not in db_text(engine)
    assert CANARY not in api.get(f"/v1/runs/{T}").text


def test_crash_path_in_reject_mode_rejects_the_span(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redaction, "_redact_span", lambda *a, **kw: 1 / 0)
    with _app(engine, tmp_path, redaction_on_error="reject") as api:
        body = api.post("/v1/spans", json={"spans": [span("1", {"x": CANARY})]}).json()
    assert (body["accepted"], body["rejected"]) == (0, 1)
    assert CANARY not in json.dumps(body)
    assert CANARY not in db_text(engine)


def test_database_errors_dont_log_sql_parameters() -> None:
    from agentflight.server.db import make_engine

    for url in ("sqlite://", "postgresql+psycopg://u@localhost:1/x"):
        assert make_engine(url).hide_parameters, url


def test_the_readme_example_policy_loads() -> None:
    import re
    import tomllib

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    block = re.search(r"```toml\n(# redaction\.toml\n.*?)```", readme, re.S)
    assert block is not None
    policy = Policy.from_dict(tomllib.loads(block.group(1)))
    assert [r.name for r in policy.rules] == [
        "credentials",
        "tokens",
        "internal-ids",
        "resource-secrets",
    ]
    out = redact(policy, {"note": "ACCT-12345678", "otel.resource.deploy.token": "t"})
    assert out.attributes["note"] == "[REDACTED:internal-ids]"
    assert "otel.resource.deploy.token" not in out.attributes


# Always-present rejected / error_message ----------------------------------------------------


def test_error_message_is_null_when_nothing_is_rejected(
    api: TestClient, client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    # With a policy that redacts, with a policy that doesn't touch the span, and without one:
    # both fields are always in the reply, and error_message is null (not "").
    redacting = api.post("/v1/spans", json={"spans": [span("1", {"note": "secret-a"})]}).json()
    untouched = api.post("/v1/spans", json={"spans": [span("2", {"note": "fine"})]}).json()
    off = client.post("/v1/spans", json={"spans": [span("3", {"note": "fine"})]}).json()
    for body in (redacting, untouched, off):
        assert body["rejected"] == 0
        assert "error_message" in body and body["error_message"] is None
    with _app(engine, tmp_path, redaction_on_error="reject") as strict:
        ok = strict.post("/v1/spans", json={"spans": [span("4", {"note": "fine"})]}).json()
    assert (ok["rejected"], ok["error_message"]) == (0, None)


def test_sdk_logs_one_warning_for_rejected_spans_and_does_not_retry_or_spool(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agentflight._spool import Spool
    from agentflight.exporter import AgentFlightExporter

    _break_one_value(monkeypatch, "app.blob")
    memory = InMemorySpanExporter()
    rec = agentflight.Recorder(memory, batch=False)
    for i in range(3):
        s = rec.tracer.start_span(f"s{i}")
        s.set_attribute("app.blob", CANARY)  # every span fails redaction
        s.end()
    spans = list(memory.get_finished_spans())

    with _app(engine, tmp_path, redaction_on_error="reject") as api:
        posts = []
        real_post = api.post

        def counting_post(*a: Any, **kw: Any) -> Any:
            posts.append(a)
            return real_post(*a, **kw)

        monkeypatch.setattr(api, "post", counting_post)
        spool = Spool(tmp_path / "spool")
        exporter = AgentFlightExporter("http://testserver", client=api, spool=spool)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="agentflight"):
            result = exporter.export(spans)
        warnings = [r for r in caplog.records if r.name.startswith("agentflight")]
        assert len(warnings) == 1 and "rejected 3 spans" in warnings[0].getMessage()
        assert "redaction failed for app.blob" in warnings[0].getMessage()
        assert CANARY not in warnings[0].getMessage()
        assert len(posts) == 1  # not retried
        assert spool.pending_spans() == 0  # not spooled
        assert result.name == "SUCCESS"  # the server answered; nothing to retry later
        exporter.export([])  # a later export doesn't resend them
        assert len(posts) == 2 and spool.pending_spans() == 0

        # Nothing rejected: no warning.
        caplog.clear()
        clean = rec.tracer.start_span("clean")
        clean.end()
        with caplog.at_level(logging.WARNING, logger="agentflight"):
            exporter.export([memory.get_finished_spans()[-1]])
        assert not [r for r in caplog.records if r.name.startswith("agentflight")]


# Change 3, split into the two behaviours -------------------------------------------------------


def test_on_divergence_live_alone_still_raises_redacted_input_error(rec: TestClient) -> None:
    recording = _recorded_lookup(rec)
    with (
        agentflight.replay(recording, on_divergence="live") as session,
        pytest.raises(RedactedInputError),
    ):
        lookup("abc-key", "delhi")
    assert session.diverged_at is None  # it didn't go live
    with (
        agentflight.fork(recording, 1, on_divergence="live"),
        pytest.raises(RedactedInputError),
    ):
        lookup("abc-key", "delhi")  # a replayed prefix step is covered too


def test_only_on_redacted_input_live_goes_live_and_it_is_recorded(rec: TestClient) -> None:
    recording = _recorded_lookup(rec)
    with (
        agentflight.replay(recording, on_redacted_input="live") as session,
        agentflight.run("opted-in") as r,
    ):
        assert lookup("abc-key", "delhi") == "result delhi"
    assert (session.diverged_at, session.on_redacted_input) == (0, "live")
    agentflight.flush()
    root = next(e for e in events(rec, r.run_id) if e["kind"] == "run")
    assert root["attributes"][A.REPLAY_ON_REDACTED_INPUT] == "live"
    step = next(e for e in events(rec, r.run_id) if e["kind"] == "tool")
    assert step["attributes"][A.REPLAY_SOURCE] == "live"
