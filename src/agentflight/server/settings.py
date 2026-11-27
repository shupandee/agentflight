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

ScopedKeys = tuple[tuple[str, frozenset[Permission]], ...]


def parse_api_keys(raw: str | None) -> ScopedKeys:
    """Parse ``AGENTFLIGHT_API_KEYS``: comma-separated ``<key>:<perm>[+<perm>...]`` entries.

    Example: ``k1:ingest,k2:read,k3:read+delete``. Errors never include the key itself.
    """
    if not raw or not raw.strip():
        return ()
    out: list[tuple[str, frozenset[Permission]]] = []
    seen: set[str] = set()
    for n, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, perms_raw = entry.rpartition(":")
        key = key.strip()
        if not sep or not key:
            raise RuntimeError(f"AGENTFLIGHT_API_KEYS entry {n}: expected <key>:<permissions>")
        perms: set[Permission] = set()
        for name in perms_raw.split("+"):
            name = name.strip().lower()
            try:
                perms.add(Permission(name))
            except ValueError:
                allowed = ", ".join(p.value for p in Permission)
                raise RuntimeError(
                    f"AGENTFLIGHT_API_KEYS entry {n}: unknown permission {name!r} "
                    f"(allowed: {allowed})"
                ) from None
        if key in seen:
            raise RuntimeError(f"AGENTFLIGHT_API_KEYS entry {n}: duplicate key")
        seen.add(key)
        out.append((key, frozenset(perms)))
    return tuple(out)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ``api_key`` grants every permission; ``scoped_keys`` are keys limited to some
    # permissions (e.g. ingest-only for recorders, read-only for the viewer).
    api_key: str | None = field(default=None, repr=False)
    scoped_keys: ScopedKeys = field(default=(), repr=False)
    auth_disabled: bool = False
    retention_enabled: bool = True
    retention_days: float = 30
    cleanup_interval_hours: float = 24

    @property
    def api_keys(self) -> dict[str, frozenset[Permission]]:
        keys = dict(self.scoped_keys)
        if self.api_key:
            keys[self.api_key] = ALL_PERMISSIONS
        return keys

    def validate(self) -> None:
        if not self.auth_disabled and not self.api_key and not self.scoped_keys:
            raise RuntimeError(
                "AgentFlight server needs AGENTFLIGHT_API_KEY (or AGENTFLIGHT_API_KEYS). For "
                "local development only, set AGENTFLIGHT_AUTH_DISABLED=true to run without "
                "authentication."
            )
        if self.api_key and any(k == self.api_key for k, _ in self.scoped_keys):
            raise RuntimeError("AGENTFLIGHT_API_KEYS repeats AGENTFLIGHT_API_KEY")
        if any(not perms for _, perms in self.scoped_keys):
            raise RuntimeError("every AGENTFLIGHT_API_KEYS entry needs a permission")
        if self.retention_days <= 0 or self.cleanup_interval_hours <= 0:
            raise RuntimeError("retention days and cleanup interval must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            api_key=os.environ.get("AGENTFLIGHT_API_KEY") or None,
            scoped_keys=parse_api_keys(os.environ.get("AGENTFLIGHT_API_KEYS")),
            auth_disabled=_bool("AGENTFLIGHT_AUTH_DISABLED", False),
            retention_enabled=_bool("AGENTFLIGHT_RETENTION_ENABLED", True),
            retention_days=float(os.environ.get("AGENTFLIGHT_RETENTION_DAYS", "30")),
            cleanup_interval_hours=float(
                os.environ.get("AGENTFLIGHT_CLEANUP_INTERVAL_HOURS", "24")
            ),
        )
