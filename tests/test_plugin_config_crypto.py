import base64
import secrets

import pytest

from pumice_server.plugin_config_crypto import (
    PluginConfigEncryptionError,
    decrypt_json,
    encrypt_json,
)


@pytest.fixture(autouse=True)
def _plugin_config_key(monkeypatch):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", key)
    return key


def test_encrypt_then_decrypt_round_trips():
    data = {"server_url": "https://immich.example.com", "id": "me@example.com", "password": "hunter2"}

    blob = encrypt_json(data)
    result = decrypt_json(blob)

    assert result == data


def test_encrypt_output_is_base64_text_not_the_plaintext():
    data = {"password": "hunter2"}

    blob = encrypt_json(data)

    assert isinstance(blob, str)
    assert "hunter2" not in blob


def test_encrypt_uses_a_fresh_iv_each_call():
    data = {"password": "hunter2"}

    assert encrypt_json(data) != encrypt_json(data)


def test_decrypt_fails_with_wrong_key(monkeypatch):
    data = {"password": "hunter2"}
    blob = encrypt_json(data)

    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())

    with pytest.raises(PluginConfigEncryptionError):
        decrypt_json(blob)


def test_decrypt_fails_on_corrupted_blob():
    data = {"password": "hunter2"}
    blob = encrypt_json(data)
    corrupted = blob[:-4] + ("A" if blob[-4] != "A" else "B") + blob[-3:]

    with pytest.raises(PluginConfigEncryptionError):
        decrypt_json(corrupted)


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("PLUGIN_CONFIG_ENCRYPTION_KEY", raising=False)

    with pytest.raises(PluginConfigEncryptionError):
        encrypt_json({"a": 1})


def test_key_wrong_length_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"too-short").decode())

    with pytest.raises(PluginConfigEncryptionError):
        encrypt_json({"a": 1})


def test_key_not_valid_base64_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", "not valid base64 !!! ???")

    with pytest.raises(PluginConfigEncryptionError):
        encrypt_json({"a": 1})
