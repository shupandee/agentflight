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


DEFAULT_STALE_AFTER_MINUTES = 60.0
MIN_KEY_CHARS = 32  # API keys shorter than this are refused at startup
DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB per request
# The SDK's OTel batch processor exports at most 512 spans per request by default.
DEFAULT_MAX_SPANS_PER_BATCH = 2048
# The SDK caps each payload at 32,000 characters by default (max_attr_chars).
DEFAULT_MAX_ATTRIBUTE_CHARS = 262_144
# OpenTelemetry's SDK keeps at most 128 attributes and 128 events per span by default.
DEFAULT_MAX_ATTRIBUTES_PER_SPAN = 1024
DEFAULT_MAX_EVENTS_PER_SPAN = 1024
DEFAULT_SESSION_HOURS = 12.0


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else float(raw)


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
    # A ``running`` run with no span received for this long is shown as ``abandoned``.
    # Computed when runs are read, so changing it needs no data fix. 0 disables it.
    stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES
    # Ingest limits. Generous defaults so nothing is unbounded; see README for sizing.
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_spans_per_batch: int = DEFAULT_MAX_SPANS_PER_BATCH
    max_attribute_chars: int = DEFAULT_MAX_ATTRIBUTE_CHARS
    max_attributes_per_span: int = DEFAULT_MAX_ATTRIBUTES_PER_SPAN
    max_events_per_span: int = DEFAULT_MAX_EVENTS_PER_SPAN
    # Viewer sessions. Without a secret, one is derived from the configured API keys, so
    # every worker and replica agrees on it; changing any key ends all sessions.
    session_secret: str | None = field(default=None, repr=False)
    session_hours: float = DEFAULT_SESSION_HOURS
    # Always mark the session cookie Secure (it is already when the request is HTTPS).
    cookie_secure: bool = False

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
        # Short keys are guessable, and there is no login rate limit yet, so refuse them.
        # Never include the key in the message.
        if self.api_key is not None and len(self.api_key) < MIN_KEY_CHARS:
            raise RuntimeError(
                f"AGENTFLIGHT_API_KEY is shorter than {MIN_KEY_CHARS} characters; "
                "generate one with: openssl rand -hex 32"
            )
        for n, (key, _) in enumerate(self.scoped_keys, start=1):
            if len(key) < MIN_KEY_CHARS:
                raise RuntimeError(
                    f"AGENTFLIGHT_API_KEYS key #{n} is shorter than {MIN_KEY_CHARS} "
                    "characters; generate one with: openssl rand -hex 32"
                )
        if self.api_key and any(k == self.api_key for k, _ in self.scoped_keys):
            raise RuntimeError("AGENTFLIGHT_API_KEYS repeats AGENTFLIGHT_API_KEY")
        if any(not perms for _, perms in self.scoped_keys):
            raise RuntimeError("every AGENTFLIGHT_API_KEYS entry needs a permission")
        if self.retention_days <= 0 or self.cleanup_interval_hours <= 0:
            raise RuntimeError("retention days and cleanup interval must be positive")
        if self.stale_after_minutes < 0:
            raise RuntimeError("AGENTFLIGHT_STALE_AFTER_MINUTES must be 0 (off) or positive")
        limits = {
            "AGENTFLIGHT_MAX_BODY_BYTES": self.max_body_bytes,
            "AGENTFLIGHT_MAX_SPANS_PER_BATCH": self.max_spans_per_batch,
            "AGENTFLIGHT_MAX_ATTRIBUTE_CHARS": self.max_attribute_chars,
            "AGENTFLIGHT_MAX_ATTRIBUTES_PER_SPAN": self.max_attributes_per_span,
            "AGENTFLIGHT_MAX_EVENTS_PER_SPAN": self.max_events_per_span,
        }
        for name, value in limits.items():
            if value <= 0:
                raise RuntimeError(f"{name} must be positive")
        if self.session_hours <= 0:
            raise RuntimeError("AGENTFLIGHT_SESSION_HOURS must be positive")
        if self.session_secret is not None and len(self.session_secret) < 32:
            raise RuntimeError("AGENTFLIGHT_SESSION_SECRET must be at least 32 characters")

    def abandoned_before_ns(self, now_ns: int) -> int | None:
        """Runs still ``running`` whose last span arrived before this are abandoned."""
        if self.stale_after_minutes <= 0:
            return None
        return now_ns - int(self.stale_after_minutes * 60 * 1_000_000_000)

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
            stale_after_minutes=_float(
                "AGENTFLIGHT_STALE_AFTER_MINUTES", DEFAULT_STALE_AFTER_MINUTES
            ),
            max_body_bytes=_int("AGENTFLIGHT_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
            max_spans_per_batch=_int(
                "AGENTFLIGHT_MAX_SPANS_PER_BATCH", DEFAULT_MAX_SPANS_PER_BATCH
            ),
            max_attribute_chars=_int(
                "AGENTFLIGHT_MAX_ATTRIBUTE_CHARS", DEFAULT_MAX_ATTRIBUTE_CHARS
            ),
            max_attributes_per_span=_int(
                "AGENTFLIGHT_MAX_ATTRIBUTES_PER_SPAN", DEFAULT_MAX_ATTRIBUTES_PER_SPAN
            ),
            max_events_per_span=_int(
                "AGENTFLIGHT_MAX_EVENTS_PER_SPAN", DEFAULT_MAX_EVENTS_PER_SPAN
            ),
            session_secret=os.environ.get("AGENTFLIGHT_SESSION_SECRET") or None,
            session_hours=_float("AGENTFLIGHT_SESSION_HOURS", DEFAULT_SESSION_HOURS),
            cookie_secure=_bool("AGENTFLIGHT_COOKIE_SECURE", False),
        )
