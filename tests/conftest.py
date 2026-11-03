from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agentflight
from agentflight.server import create_app
from agentflight.server.db import make_engine
from agentflight.server.models import Base


@pytest.fixture
def client() -> Iterator[TestClient]:
    # SQLite in-memory by default; CI also runs the suite against Postgres.
    engine = make_engine(os.environ.get("AGENTFLIGHT_TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(create_app(engine)) as c:
        yield c
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def memory_exporter() -> Iterator[InMemorySpanExporter]:
    """SDK wired to an in-memory exporter, for pure SDK tests."""
    exporter = InMemorySpanExporter()
    agentflight.configure(exporter=exporter, batch=False)
    yield exporter
    agentflight.shutdown()


@pytest.fixture
def recorded(client: TestClient) -> Iterator[TestClient]:
    """SDK wired to the real ingest API through the AgentFlight exporter."""
    exporter = agentflight.AgentFlightExporter("http://testserver", client=client)
    agentflight.configure(exporter=exporter, batch=False)
    yield client
    agentflight.shutdown()
