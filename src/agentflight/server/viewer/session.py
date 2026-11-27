"""Signed, read-only viewer sessions.

The browser can't send ``Authorization`` headers on page loads, so the viewer signs a
person in once with a key that has the ``read`` permission and stores a signed session in a
cookie. The cookie never holds the key: it holds an HMAC fingerprint of it, the
permissions the session may use (only ``read``) and an expiry, signed with the server's
session secret. Every request re-checks the fingerprint against the configured keys, so
removing a key ends its sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request

from agentflight.server.settings import Permission, Settings

COOKIE_NAME = "agentflight_session"
COOKIE_PATH = "/ui"
SESSION_VERSION = 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class ViewerSession:
    fingerprint: str
    expires_at: int  # Unix seconds


class SessionSigner:
    def __init__(self, secret: bytes, max_age_seconds: int) -> None:
        self._secret = secret
        self.max_age_seconds = max_age_seconds

    def fingerprint(self, key: str) -> str:
        """Identifies a key without storing or revealing it."""
        return hmac.new(self._secret, b"key:" + key.encode(), hashlib.sha256).hexdigest()

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, b"session:" + body.encode(), hashlib.sha256).digest())

    def issue(self, key: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = {
            "v": SESSION_VERSION,
            "fp": self.fingerprint(key),
            "perms": [Permission.READ.value],
            "exp": int(now) + self.max_age_seconds,
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, now: float | None = None) -> ViewerSession | None:
        now = time.time() if now is None else now
        body, sep, signature = token.partition(".")
        if not sep or not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
            fingerprint, expires_at = str(payload["fp"]), int(payload["exp"])
            version, perms = payload["v"], payload["perms"]
        except (ValueError, KeyError, TypeError):
            return None
        if version != SESSION_VERSION or perms != [Permission.READ.value] or expires_at <= now:
            return None
        return ViewerSession(fingerprint=fingerprint, expires_at=expires_at)


_DERIVE_LABEL = b"agentflight viewer session secret v1"


def derive_session_secret(keys: Mapping[str, frozenset[Permission]]) -> bytes:
    """The default session secret: an HMAC of every configured key.

    Every worker and replica with the same keys derives the same secret, so sessions work
    across them with no extra setting. Adding, removing or changing a key changes the
    secret and ends every session. ``AGENTFLIGHT_SESSION_SECRET`` overrides it.
    """
    material = "\n".join(sorted(keys)).encode()
    return hmac.new(_DERIVE_LABEL, material, hashlib.sha256).digest()


def read_key_fingerprints(signer: SessionSigner, settings: Settings) -> frozenset[str]:
    return frozenset(
        signer.fingerprint(key)
        for key, perms in settings.api_keys.items()
        if Permission.READ in perms
    )


def current_session(request: Request) -> ViewerSession | None:
    """The request's valid viewer session, if any."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    signer: SessionSigner = request.app.state.session_signer
    session = signer.verify(token)
    if session is None:
        return None
    if session.fingerprint not in read_key_fingerprints(signer, request.app.state.settings):
        return None  # the key was removed or lost its read permission
    return session


def is_https(request: Request, settings: Settings) -> bool:
    return settings.cookie_secure or request.url.scheme == "https"
