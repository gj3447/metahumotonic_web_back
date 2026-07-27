"""Credential vault for the MCP registry — PBKDF2-SHA256 → Fernet.

Design (H-04): the registry publishes ONE encrypted blob. Every service's real
credentials live inside it; the symmetric key is derived from a single registry
password. Public surfaces (``GET /api/mcp/vault``, the manifest, llms.txt) only
ever carry the ciphertext plus the KDF parameters needed to derive the key —
never a plaintext secret.

Decryption recipe (what llms.txt / the manifest tell agents to do)::

    doc  = GET /api/mcp/vault            # {kdf: {iterations, salt(b64)}, blob}
    key  = base64url(PBKDF2-HMAC-SHA256(password, b64decode(salt), iterations, 32))
    data = json.loads(Fernet(key).decrypt(doc["blob"]))   # {service: {...creds}}

Wrong password → ``cryptography.fernet.InvalidToken`` → :class:`VaultError`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

VAULT_ID = "credential_vault"
KDF_NAME = "PBKDF2-SHA256"
CIPHER_NAME = "fernet"
# OWASP 2023 recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


class VaultError(RuntimeError):
    """User-facing vault failure — bad password, corrupted blob, bad doc."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 → 32 bytes → urlsafe-b64 (a valid Fernet key)."""
    if not password:
        raise VaultError("empty vault password")
    raw = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations), dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def encrypt_payload(
    payload: dict[str, Any], password: str, *, iterations: int = PBKDF2_ITERATIONS
) -> dict[str, Any]:
    """Encrypt ``{service: {...creds}}`` into the stored vault document."""
    if not isinstance(payload, dict) or not payload:
        raise VaultError("vault payload must be a non-empty {service: creds} object")
    from cryptography.fernet import Fernet

    salt = os.urandom(SALT_BYTES)
    token = Fernet(derive_key(password, salt, iterations)).encrypt(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return {
        "_id": VAULT_ID,
        "kind": "vault",
        "version": 1,
        "cipher": CIPHER_NAME,
        "kdf": {
            "name": KDF_NAME,
            "iterations": int(iterations),
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "blob": token.decode("ascii"),
        # Service names are public (they are in the manifest anyway); values never.
        "services": sorted(payload.keys()),
        "updated_at": _now_iso(),
    }


def _require_fields(doc: dict[str, Any]) -> tuple[dict[str, Any], str]:
    kdf = doc.get("kdf") or {}
    blob = doc.get("blob") or ""
    if not blob or not kdf.get("salt") or not kdf.get("iterations"):
        raise VaultError("vault document is incomplete (kdf.salt/iterations/blob)")
    if kdf.get("name", KDF_NAME) != KDF_NAME:
        raise VaultError(f"unsupported kdf: {kdf.get('name')!r}")
    if doc.get("cipher", CIPHER_NAME) != CIPHER_NAME:
        raise VaultError(f"unsupported cipher: {doc.get('cipher')!r}")
    return kdf, blob


def decrypt_payload(doc: dict[str, Any], password: str) -> dict[str, Any]:
    """Decrypt a vault document. Raises :class:`VaultError` on wrong password."""
    from cryptography.fernet import Fernet, InvalidToken

    kdf, blob = _require_fields(doc)
    try:
        salt = base64.b64decode(kdf["salt"])
    except (ValueError, TypeError) as e:
        raise VaultError("vault kdf.salt is not valid base64") from e
    key = derive_key(password, salt, int(kdf["iterations"]))
    try:
        plain = Fernet(key).decrypt(blob.encode("ascii"))
    except InvalidToken as e:
        raise VaultError("decryption failed — wrong password or corrupted vault") from e
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise VaultError("vault plaintext is not valid JSON") from e
    if not isinstance(payload, dict):
        raise VaultError("vault payload is not a JSON object")
    return payload


def public_view(doc: dict[str, Any]) -> dict[str, Any]:
    """The API-safe projection: ciphertext + KDF params only. No _id/kind, and
    (defensively) no field that is not part of the public vault contract."""
    _require_fields(doc)
    return {
        "version": doc.get("version", 1),
        "cipher": doc.get("cipher", CIPHER_NAME),
        "kdf": {
            "name": doc["kdf"].get("name", KDF_NAME),
            "iterations": int(doc["kdf"]["iterations"]),
            "salt": doc["kdf"]["salt"],
        },
        "blob": doc["blob"],
        "services": sorted(doc.get("services") or []),
        "updated_at": doc.get("updated_at"),
    }
