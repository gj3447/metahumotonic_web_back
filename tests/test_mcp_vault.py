"""Tests for app/mcp_vault — the one-password credential vault (H-04).

Proves the completion criteria: the registry password decrypts every service's
credentials, a wrong password fails, and the stored/public document carries no
plaintext secret.
"""

from __future__ import annotations

import json

import pytest

from app import mcp_vault

PASSWORD = "312447"
PAYLOAD = {
    "redis": {"urls": ["redis://default:redispassword@127.0.0.1:16379/0"]},
    "mongodb": {"urls": ["mongodb://mongo:mongopassword@127.0.0.1:37017/"]},
    "bhgman-neo4j": {"env": {"NEO4J_URI": "bolt://localhost:17687", "NEO4J_PASSWORD": "neo4jpassword"}},
}


def test_roundtrip_recovers_every_service():
    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    recovered = mcp_vault.decrypt_payload(doc, PASSWORD)
    assert recovered == PAYLOAD
    assert set(recovered) == {"redis", "mongodb", "bhgman-neo4j"}


def test_wrong_password_fails():
    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    with pytest.raises(mcp_vault.VaultError, match="wrong password"):
        mcp_vault.decrypt_payload(doc, "000000")


def test_tampered_blob_fails():
    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    doc["blob"] = doc["blob"][:-8] + "AAAAAAA="
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.decrypt_payload(doc, PASSWORD)


def test_document_and_public_view_carry_no_plaintext():
    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    view = mcp_vault.public_view(doc)
    for text in (json.dumps(doc), json.dumps(view)):
        assert "redispassword" not in text
        assert "mongopassword" not in text
        assert "neo4jpassword" not in text
        assert PASSWORD not in text
    # everything an agent needs to derive the key IS present
    assert view["kdf"]["name"] == "PBKDF2-SHA256"
    assert view["kdf"]["iterations"] >= 100_000
    assert view["kdf"]["salt"]
    assert view["cipher"] == "fernet"
    assert view["blob"]
    assert view["services"] == sorted(PAYLOAD)
    assert "_id" not in view and "kind" not in view


def test_stored_document_shape():
    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    assert doc["_id"] == mcp_vault.VAULT_ID == "credential_vault"
    assert doc["kind"] == "vault"
    assert doc["kdf"]["name"] == "PBKDF2-SHA256"
    assert doc["updated_at"]


def test_empty_payload_and_empty_password_rejected():
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.encrypt_payload({}, PASSWORD)
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.encrypt_payload(PAYLOAD, "")


def test_incomplete_document_rejected():
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.decrypt_payload({"blob": "x"}, PASSWORD)
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.public_view({"kdf": {"iterations": 1}})


def test_manual_recipe_decrypts():
    """The documented agent recipe (stdlib PBKDF2 + Fernet) works verbatim."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    doc = mcp_vault.encrypt_payload(PAYLOAD, PASSWORD)
    key = base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac(
            "sha256",
            PASSWORD.encode(),
            base64.b64decode(doc["kdf"]["salt"]),
            doc["kdf"]["iterations"],
            dklen=32,
        )
    )
    assert json.loads(Fernet(key).decrypt(doc["blob"])) == PAYLOAD
