"""TDD for `find_live_file_by_content_hash` -- the repository-layer half of PR6 of
#14_옵시디언싱크_정렬_구현계획.md (pumice repo): content-hash-based upload dedup, confirmed via
direct obsidian.asar analysis to be how real Obsidian Sync's own `push` op works (the client
declares a hash/size up front; the server may skip the byte transfer entirely if it already has
that exact content).

Scoped to (owner_username, vault_id) only -- deliberately not cross-vault or cross-owner. For an
E2EE-enabled vault, content_hash is a hash of *ciphertext* (the server is zero-knowledge and
never sees plaintext), so two different owners' hashes for the same plaintext would never match
anyway (different keys produce different ciphertext) -- but within one owner's one vault, the
IV is deterministic (derived from the plaintext hash), so identical plaintext always re-encrypts
to identical ciphertext, and matching is safe.
"""

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def test_find_live_file_by_content_hash_returns_none_when_nothing_matches(repo):
    assert repo.find_live_file_by_content_hash("alice", "vault-1", "deadbeef") is None


def test_find_live_file_by_content_hash_finds_a_live_match(repo):
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 5, "content_hash": "h1", "is_deleted": False})

    result = repo.find_live_file_by_content_hash("alice", "vault-1", "h1")

    assert result["path"] == "a.md"
    assert result["content_hash"] == "h1"
    assert result["size_bytes"] == 5


def test_find_live_file_by_content_hash_ignores_a_deleted_file_with_the_same_hash(repo):
    # A tombstone with a real hash shouldn't happen in practice today (every tombstone-creation
    # site blanks content_hash to "" -- see the design doc's investigation), but this pins the
    # intended semantic regardless: dedup only ever reuses bytes that are actually still live on
    # disk somewhere, never a deleted file's old content.
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 5, "content_hash": "h1", "is_deleted": True})

    assert repo.find_live_file_by_content_hash("alice", "vault-1", "h1") is None


def test_find_live_file_by_content_hash_never_matches_the_empty_string_sentinel(repo):
    # "" is the tombstone/placeholder sentinel used elsewhere in this codebase, not a real hash
    # -- a bunch of unrelated tombstones sharing "" must never look like a dedup hit.
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 0, "content_hash": "", "is_deleted": False})

    assert repo.find_live_file_by_content_hash("alice", "vault-1", "") is None


def test_find_live_file_by_content_hash_is_scoped_by_owner_and_vault(repo):
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 5, "content_hash": "h1", "is_deleted": False})

    assert repo.find_live_file_by_content_hash("bob", "vault-1", "h1") is None
    assert repo.find_live_file_by_content_hash("alice", "vault-2", "h1") is None


def test_find_live_file_by_content_hash_returns_one_of_possibly_several_matches(repo):
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 5, "content_hash": "h1", "is_deleted": False})
    repo.save_one("alice", "vault-1", "b.md", {"path": "b.md", "modified_at_ms": 2, "size_bytes": 5, "content_hash": "h1", "is_deleted": False})

    result = repo.find_live_file_by_content_hash("alice", "vault-1", "h1")

    assert result["path"] in {"a.md", "b.md"}
