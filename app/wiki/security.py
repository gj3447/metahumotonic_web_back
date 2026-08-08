"""Small HMAC session capability tokens for HTTP, CLI, and MCP clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .domain import WikiError

PUBLIC_SCOPES = frozenset({"wiki:read", "wiki:edit", "wiki:submit", "wiki:report"})


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    actor_id: str
    display_name: str
    actor_kind: str
    agent_url: str | None
    scopes: frozenset[str]
    csrf_token: str
    expires_at: datetime


class SessionSigner:
    def __init__(self, secret: bytes, *, ttl: timedelta = timedelta(hours=12)) -> None:
        if len(secret) < 32:
            raise ValueError("wiki session secret must be at least 32 bytes")
        self._secret = secret
        self._ttl = ttl

    def issue(
        self,
        *,
        actor_id: str,
        display_name: str,
        actor_kind: str,
        agent_url: str | None,
        now: datetime,
    ) -> tuple[str, SessionIdentity]:
        scopes = PUBLIC_SCOPES
        expires_at = now + self._ttl
        identity = SessionIdentity(
            actor_id=actor_id,
            display_name=display_name,
            actor_kind=actor_kind,
            agent_url=agent_url,
            scopes=frozenset(scopes),
            csrf_token=secrets.token_urlsafe(24),
            expires_at=expires_at,
        )
        payload = {
            "v": 1,
            "actor_id": actor_id,
            "display_name": display_name,
            "actor_kind": actor_kind,
            "agent_url": agent_url,
            "scopes": sorted(identity.scopes),
            "csrf": identity.csrf_token,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        encoded = _b64(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}", identity

    def verify(self, token: str, *, now: datetime) -> SessionIdentity:
        try:
            encoded, signature = token.split(".", 1)
            expected = _b64(
                hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            payload: dict[str, Any] = json.loads(_unb64(encoded))
            if payload.get("v") != 1 or int(payload["exp"]) <= int(now.timestamp()):
                raise ValueError("expired or unsupported token")
            scopes = frozenset(str(scope) for scope in payload["scopes"])
            if not scopes or not scopes.issubset(PUBLIC_SCOPES):
                raise ValueError("invalid scopes")
            return SessionIdentity(
                actor_id=str(payload["actor_id"]),
                display_name=str(payload.get("display_name", "Anonymous")),
                actor_kind=str(payload.get("actor_kind", "human")),
                agent_url=str(payload["agent_url"])
                if payload.get("agent_url")
                else None,
                scopes=scopes,
                csrf_token=str(payload["csrf"]),
                expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WikiError(
                "invalid_session", "invalid or expired wiki session", status_code=401
            ) from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
