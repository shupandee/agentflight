"""ASGI entrypoint: ``uvicorn agentflight.server.main:app``."""

from agentflight.server.app import create_app

app = create_app()
