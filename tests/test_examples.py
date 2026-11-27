"""The integration examples run offline; keep them working."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentflight import attributes as A

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def kinds(exporter: InMemorySpanExporter) -> list[str]:
    return [str(s.attributes.get(A.KIND)) for s in exporter.get_finished_spans()]


def test_openai_example_offline(
    memory_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    example = load("openai_agent")
    client = example.make_client()
    assert example.agent(client, "What is the weather in Delhi?") == "Final: 31C, sunny"
    assert example.agent(client, "What is the weather in Atlantis?").startswith("Final: error")
    assert kinds(memory_exporter).count(A.LLM) == 4
    assert kinds(memory_exporter).count(A.TOOL) == 2


def test_langgraph_example(memory_exporter: InMemorySpanExporter) -> None:
    pytest.importorskip("langgraph")
    assert load("langgraph_agent").main() == "It is 31C and sunny in Delhi."
    assert sorted(set(kinds(memory_exporter))) == [A.LLM, A.NODE, A.RUN, A.TOOL]
