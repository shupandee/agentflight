"""API-key authentication and header redaction."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Iterable, Mapping

from fastapi import HTTPException, Request

from agentflight.server.settings import Permission, Settings

SENSITIVE_HEADERS = frozenset({"authorization", "x-agentflight-key", "cookie", "set-cookie"})
REDACTED = "[REDACTED]"


def redact_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """Copy of headers safe to log: credential-bearing values replaced."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {k: (REDACTED if k.lower() in SENSITIVE_HEADERS else v) for k, v in items}


class RedactingFilter(logging.Filter):
    """Scrubs the configured key from any log record, as a last line of defence."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            if any(s in msg for s in self.secrets):
                for s in self.secrets:
                    msg = msg.replace(s, REDACTED)
                record.msg, record.args = msg, None
        return True


def presented_key(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        return None
    return request.headers.get("x-agentflight-key")


def _match(key: str, keys: Mapping[str, frozenset[Permission]]) -> frozenset[Permission]:
    """Constant-time comparison against every configured key."""
    granted: frozenset[Permission] = frozenset()
    candidate = key.encode()
    for configured, perms in keys.items():
        if hmac.compare_digest(candidate, configured.encode()):
            granted = perms
    return granted


def permissions_for(key: str, keys: Mapping[str, frozenset[Permission]]) -> frozenset[Permission]:
    """Permissions a presented key grants (empty if it matches no configured key)."""
    return _match(key, keys)


def require(permission: Permission) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        settings: Settings = request.app.state.settings
        if settings.auth_disabled:
            return
        key = presented_key(request)
        if key is None:
            raise HTTPException(
                status_code=401,
                detail="missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        perms = _match(key, settings.api_keys)
        if not perms:
            raise HTTPException(
                status_code=401,
                detail="invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if permission not in perms:
            raise HTTPException(status_code=403, detail=f"key lacks {permission} permission")

    return dependency
