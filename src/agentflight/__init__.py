"""AgentFlight: a black box recorder for AI agents."""

from agentflight.exporter import AgentFlightExporter
from agentflight.replayer import (
    ForkSession,
    Recording,
    ReplayDivergence,
    ReplayedError,
    ReplayError,
    ReplaySession,
    fork,
    replay,
)
from agentflight.sdk import (
    LLMCall,
    Recorder,
    RunHandle,
    configure,
    decision,
    error,
    failure_id,
    flush,
    get_recorder,
    llm_call,
    redact_fields,
    run,
    shutdown,
    state_change,
    tool,
)

__all__ = [
    "AgentFlightExporter",
    "ForkSession",
    "LLMCall",
    "Recorder",
    "Recording",
    "ReplayDivergence",
    "ReplayError",
    "ReplaySession",
    "ReplayedError",
    "RunHandle",
    "configure",
    "decision",
    "error",
    "failure_id",
    "flush",
    "fork",
    "get_recorder",
    "llm_call",
    "redact_fields",
    "replay",
    "run",
    "shutdown",
    "state_change",
    "tool",
]

__version__ = "0.1.0"
