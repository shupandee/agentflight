"""Span attribute keys used by the AgentFlight SDK and server."""

from __future__ import annotations

PREFIX = "agentflight."

KIND = "agentflight.kind"
RUN_NAME = "agentflight.run.name"
RUN_METADATA = "agentflight.run.metadata"

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

ERROR_TYPE = "agentflight.error.type"
ERROR_MESSAGE = "agentflight.error.message"
# Same id on every event caused by one underlying exception (tool span, agent log, run).
FAILURE_ID = "agentflight.failure.id"

# JSON object: attribute key -> {original_length, stored_length, reason}
TRUNCATED = "agentflight.truncated"

# Event kinds
RUN = "run"
LLM = "llm"
TOOL = "tool"
DECISION = "decision"
STATE = "state"
ERROR = "error"
