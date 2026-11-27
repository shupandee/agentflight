"""Span attribute keys used by the AgentFlight SDK and server."""

from __future__ import annotations

PREFIX = "agentflight."

KIND = "agentflight.kind"
RUN_NAME = "agentflight.run.name"
RUN_METADATA = "agentflight.run.metadata"
# On the run span: spans of this run the SDK dropped because its export queue was full,
# counted up to the moment the run ended (best effort; see the README).
RUN_DROPPED_SPANS = "agentflight.run.dropped_spans"

LLM_MODEL = "agentflight.llm.model"
LLM_MESSAGES = "agentflight.llm.messages"
LLM_RESPONSE = "agentflight.llm.response"
LLM_TOKENS_IN = "agentflight.llm.tokens_in"
LLM_TOKENS_OUT = "agentflight.llm.tokens_out"
LLM_COST_USD = "agentflight.llm.cost_usd"
LLM_PARAMS = "agentflight.llm.params"

# Which integration produced a span (e.g. "openai", "langgraph"); unset for manual calls.
INTEGRATION = "agentflight.integration"

TOOL_NAME = "agentflight.tool.name"
TOOL_ARGS = "agentflight.tool.args"
TOOL_RESULT = "agentflight.tool.result"

DECISION_NAME = "agentflight.decision.name"
DECISION_CHOICE = "agentflight.decision.choice"
DECISION_REASON = "agentflight.decision.reason"

STATE_KEY = "agentflight.state.key"
STATE_OLD = "agentflight.state.old"
STATE_NEW = "agentflight.state.new"

NODE_NAME = "agentflight.node.name"
NODE_STEP = "agentflight.node.step"
NODE_INPUT = "agentflight.node.input"
NODE_OUTPUT = "agentflight.node.output"

ERROR_TYPE = "agentflight.error.type"
ERROR_MESSAGE = "agentflight.error.message"
# Same id on every event caused by one underlying exception (tool span, agent log, run).
FAILURE_ID = "agentflight.failure.id"

# Replay and fork (see agentflight.replayer)
REPLAY_SOURCE_RUN = "agentflight.replay.source_run"  # on the run: the recording it replays
REPLAY_MODE = "agentflight.replay.mode"  # on the run: "replay" or "fork"
REPLAY_FORK_STEP = "agentflight.replay.fork_step"  # on the run, for forks
# On the run, with on_divergence="live": first step that didn't match, and why
REPLAY_DIVERGED_AT = "agentflight.replay.diverged_at"
REPLAY_DIVERGENCE = "agentflight.replay.divergence"
REPLAY_STEP = "agentflight.replay.step"  # on llm/tool spans: step index in the recording
# On llm/tool spans: "cached" (served from the recording), "override" (fork output) or "live"
REPLAY_SOURCE = "agentflight.replay.source"

# JSON object: attribute key -> {original_length, stored_length, reason}
TRUNCATED = "agentflight.truncated"

# Event kinds
RUN = "run"
LLM = "llm"
TOOL = "tool"
DECISION = "decision"
STATE = "state"
ERROR = "error"
NODE = "node"  # a graph or graph node (LangGraph)
