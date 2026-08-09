"""Encryption at rest for plugin_config entries -- the server_url/token-shaped
credentials a plugin needs to do its job (e.g. pumice-plugin-immich's own Immich API key), stored
once by an admin/vault-owner and read back on every subsequent publish render.

Unlike e2ee.py (a per-request key the client derives and that this server never persists), this
uses a single server-held key from the PLUGIN_CONFIG_ENCRYPTION_KEY environment variable --
there's no client-side interaction at read time to derive a key from; the server has to be able
to decrypt this on its own, on every publish request, indefinitely.

Generate a key with:
    python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH_BYTES = 12
_KEY_LENGTH_BYTES = 32


class PluginConfigEncryptionError(Exception):
    pass


def _get_key() -> bytes:
    raw = os.getenv("PLUGIN_CONFIG_ENCRYPTION_KEY")
    if not raw:
        raise PluginConfigEncryptionError(
            "PLUGIN_CONFIG_ENCRYPTION_KEY is not set -- required to store or read plugin config "
            "(the server_url/token-shaped credentials a plugin needs)."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as e:
        raise PluginConfigEncryptionError(f"PLUGIN_CONFIG_ENCRYPTION_KEY is not valid base64: {e}") from e
    if len(key) != _KEY_LENGTH_BYTES:
        raise PluginConfigEncryptionError(
            f"PLUGIN_CONFIG_ENCRYPTION_KEY must decode to exactly {_KEY_LENGTH_BYTES} bytes (AES-256), got {len(key)}"
        )
    return key


def encrypt_json(data: dict) -> str:
    """Returns a base64-encoded (IV || ciphertext-with-tag) string, not raw bytes -- stored as
    plain TEXT in the DB rather than a binary column, since this codebase supports several DB
    backends (sqlite/mysql/mariadb/postgresql/cubrid) and binary-column handling isn't uniformly
    reliable across all of their drivers; the size overhead of base64 is irrelevant for a blob
    this small. A fresh random IV each call (unlike e2ee.py's deterministic one) -- there's no
    cross-writer dedup concern for server-held config the way there is for synced file content.
    """
    key = _get_key()
    iv = os.urandom(_IV_LENGTH_BYTES)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(data).encode("utf-8")
    ciphertext = aesgcm.encrypt(iv, plaintext, None)
    return base64.urlsafe_b64encode(iv + ciphertext).decode("ascii")


def decrypt_json(blob_b64: str) -> dict:
    key = _get_key()
    try:
        blob = base64.urlsafe_b64decode(blob_b64)
    except Exception as e:
        raise PluginConfigEncryptionError(f"Invalid encrypted plugin config blob: {e}") from e

    if len(blob) < _IV_LENGTH_BYTES:
        raise PluginConfigEncryptionError("Invalid encrypted plugin config blob (too short)")

    iv = blob[:_IV_LENGTH_BYTES]
    ciphertext = blob[_IV_LENGTH_BYTES:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
    except Exception as e:
        raise PluginConfigEncryptionError(f"Failed to decrypt plugin config (wrong key, or corrupted data): {e}") from e

    return json.loads(plaintext.decode("utf-8"))
