"""Server configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum


class Permission(StrEnum):
    INGEST = "ingest"
    READ = "read"
    DELETE = "delete"


ALL_PERMISSIONS = frozenset(Permission)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # A single key that currently grants every permission. ``api_keys`` maps
    # key -> permissions so ingest-only / read-only keys can be added later without
    # changing the auth dependency.
    api_key: str | None = field(default=None, repr=False)
    auth_disabled: bool = False
    retention_enabled: bool = True
    retention_days: float = 30
    cleanup_interval_hours: float = 24

    @property
    def api_keys(self) -> dict[str, frozenset[Permission]]:
        return {self.api_key: ALL_PERMISSIONS} if self.api_key else {}

    def validate(self) -> None:
        if not self.auth_disabled and not self.api_key:
            raise RuntimeError(
                "AgentFlight server needs AGENTFLIGHT_API_KEY. For local development only, "
                "set AGENTFLIGHT_AUTH_DISABLED=true to run without authentication."
            )
        if self.retention_days <= 0 or self.cleanup_interval_hours <= 0:
            raise RuntimeError("retention days and cleanup interval must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            api_key=os.environ.get("AGENTFLIGHT_API_KEY") or None,
            auth_disabled=_bool("AGENTFLIGHT_AUTH_DISABLED", False),
            retention_enabled=_bool("AGENTFLIGHT_RETENTION_ENABLED", True),
            retention_days=float(os.environ.get("AGENTFLIGHT_RETENTION_DAYS", "30")),
            cleanup_interval_hours=float(
                os.environ.get("AGENTFLIGHT_CLEANUP_INTERVAL_HOURS", "24")
            ),
        )
