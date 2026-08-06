"""Mirrors syncClient.ts's getE2eeKey()/encryptData()/decryptData() exactly -- this server never
generates E2EE ciphertext on its own initiative (it's a "dumb" encrypted-blob store, see #5-1 in
MCP_STREAMABLE_HTTP_구현_계획.md); it only decrypts what a real client already produced (for
read_file_version/read_note/search_notes/search_by_frontmatter) or encrypts content an MCP caller
explicitly asked to write (write_note, when the caller supplies e2ee_password). The derived key is
never persisted anywhere -- callers must derive it fresh per request and let it fall out of scope
when done.

Wire format (must match syncClient.ts's encryptData() exactly): 12-byte IV, then 16-byte GCM tag,
then ciphertext. The IV is not random -- the client derives it deterministically from
SHA-256(plaintext)[:12] so that re-encrypting unchanged content always reproduces the same
ciphertext (see syncClient.ts's own comment on this). encrypt() replicates that derivation exactly
(not a random IV) so that if a real client later independently encrypts the same plaintext, it
produces byte-identical ciphertext -- the hash-based dedup/change-detection this whole sync system
relies on depends on that invariant holding across every writer, not just real clients.
"""

import hashlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_PBKDF2_ITERATIONS = 100_000
_KEY_LENGTH_BYTES = 32
_IV_LENGTH_BYTES = 12
_TAG_LENGTH_BYTES = 16


class DecryptionError(Exception):
    pass


def derive_key(password: str, vault_id: str) -> bytes:
    salt = f"obsidian-sync-salt-{vault_id}".encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH_BYTES,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def decrypt(wire_blob: bytes, key: bytes) -> bytes:
    header_len = _IV_LENGTH_BYTES + _TAG_LENGTH_BYTES
    if len(wire_blob) < header_len:
        raise DecryptionError("Invalid encrypted buffer size")

    iv = wire_blob[:_IV_LENGTH_BYTES]
    tag = wire_blob[_IV_LENGTH_BYTES:header_len]
    ciphertext = wire_blob[header_len:]

    aesgcm = AESGCM(key)
    try:
        # cryptography's AESGCM.decrypt expects ciphertext with the tag appended, same layout
        # Web Crypto's own combined encrypt() output uses -- matches what encryptData() split apart.
        return aesgcm.decrypt(iv, ciphertext + tag, None)
    except InvalidTag as e:
        raise DecryptionError("Decryption failed: wrong password or corrupted data") from e


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    iv = hashlib.sha256(plaintext).digest()[:_IV_LENGTH_BYTES]
    aesgcm = AESGCM(key)
    # AESGCM.encrypt returns ciphertext with the 16-byte tag appended -- split it apart to match
    # the wire format's [iv][tag][ciphertext] layout (decrypt()'s exact inverse).
    ciphertext_and_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext, tag = ciphertext_and_tag[:-_TAG_LENGTH_BYTES], ciphertext_and_tag[-_TAG_LENGTH_BYTES:]
    return iv + tag + ciphertext
