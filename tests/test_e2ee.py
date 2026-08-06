"""Server-side mirror of syncClient.ts's getE2eeKey()/decryptData() -- must produce byte-identical
results to the real client, since the server never generates ciphertext itself, only decrypts what
a real client already encrypted. test_decrypt_matches_real_client_ciphertext uses a reference
vector generated from the actual client algorithm (Node's Web Crypto, the same API the Obsidian
plugin runs on) rather than a self-consistent Python-only round trip, so a subtle mismatch against
the real client (wrong salt format, wrong tag/IV layout, wrong PBKDF2 params) would actually be
caught. See MCP_STREAMABLE_HTTP_구현_계획.md 5-1절.
"""

import base64

import pytest

from pumice_server.e2ee import DecryptionError, decrypt, derive_key, encrypt


def test_derive_key_is_32_bytes():
    key = derive_key("some-password", "some-vault")
    assert len(key) == 32


def test_derive_key_is_deterministic():
    assert derive_key("pw", "vault1") == derive_key("pw", "vault1")


def test_derive_key_differs_by_password():
    assert derive_key("pw1", "vault1") != derive_key("pw2", "vault1")


def test_derive_key_differs_by_vault_id():
    assert derive_key("pw", "vault1") != derive_key("pw", "vault2")


def test_decrypt_matches_real_client_ciphertext():
    # Generated from the actual client-side algorithm (syncClient.ts's getE2eeKey()/encryptData(),
    # run via Node's Web Crypto) for password="test-password-123", vault_id="test-vault",
    # plaintext="hello e2ee world" -- see the git history of this test for the generating script.
    wire_blob = base64.b64decode("7cVv31PI6om44N14q580ME3T9hrbP9Hl5KTNpO0ZCYETl0vrE9HjB4z2coc=")
    key = derive_key("test-password-123", "test-vault")

    plaintext = decrypt(wire_blob, key)

    assert plaintext == b"hello e2ee world"


def test_decrypt_rejects_too_short_blob():
    key = derive_key("pw", "vault1")
    with pytest.raises(DecryptionError):
        decrypt(b"too-short", key)


def test_decrypt_rejects_wrong_key():
    wire_blob = base64.b64decode("7cVv31PI6om44N14q580ME3T9hrbP9Hl5KTNpO0ZCYETl0vrE9HjB4z2coc=")
    wrong_key = derive_key("wrong-password", "test-vault")
    with pytest.raises(DecryptionError):
        decrypt(wire_blob, wrong_key)


def test_decrypt_rejects_tampered_ciphertext():
    wire_blob = bytearray(base64.b64decode("7cVv31PI6om44N14q580ME3T9hrbP9Hl5KTNpO0ZCYETl0vrE9HjB4z2coc="))
    wire_blob[-1] ^= 0xFF  # flip a bit in the ciphertext tail
    key = derive_key("test-password-123", "test-vault")
    with pytest.raises(DecryptionError):
        decrypt(bytes(wire_blob), key)


def test_encrypt_matches_real_client_ciphertext():
    # Same reference vector as test_decrypt_matches_real_client_ciphertext, used in the opposite
    # direction: encrypting "hello e2ee world" under this exact password/vault_id must reproduce
    # the exact same bytes the real client produced -- this only holds if the IV derivation
    # (SHA-256(plaintext)[:12], not random) matches the client exactly. A random IV would still
    # produce *decryptable* ciphertext, but different bytes here, and would silently break the
    # hash-based dedup that depends on identical content -> identical ciphertext across devices.
    expected_wire_blob = base64.b64decode("7cVv31PI6om44N14q580ME3T9hrbP9Hl5KTNpO0ZCYETl0vrE9HjB4z2coc=")
    key = derive_key("test-password-123", "test-vault")

    wire_blob = encrypt(b"hello e2ee world", key)

    assert wire_blob == expected_wire_blob


def test_encrypt_output_round_trips_through_decrypt():
    key = derive_key("pw", "vault1")
    wire_blob = encrypt(b"some note content", key)
    assert decrypt(wire_blob, key) == b"some note content"


def test_encrypt_is_deterministic_for_identical_plaintext_and_key():
    key = derive_key("pw", "vault1")
    assert encrypt(b"same content", key) == encrypt(b"same content", key)


def test_encrypt_differs_for_different_plaintext():
    key = derive_key("pw", "vault1")
    assert encrypt(b"content a", key) != encrypt(b"content b", key)
