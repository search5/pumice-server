"""TDD for SyncServiceServicer.GetSize/PurgeVault/GetUsernames -- new RPC methods added while
re-implementing Obsidian core's real Sync WS commands more faithfully (2026-08, see
#11_websocket_동기화_프로토콜_설계.md's re-analysis and pumice-server/llm-wiki/06-*.md for the
full write-up of what was and wasn't ported over).
"""

import asyncio
import os

import pytest

from pumice_server import wire_types
from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakeContext:
    def __init__(self, token="tok"):
        self._token = token

    def invocation_metadata(self):
        return [("authorization", f"Bearer {self._token}")]

    async def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")


# ─── GetSize ────────────────────────────────────────────────────────────────────────────────


def test_get_size_returns_the_vault_and_total_size_with_no_limit(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    repo.save_one("alice", "vault1", "a.md", {"modified_at_ms": 1, "size_bytes": 100, "content_hash": "h1", "is_deleted": False})
    repo.save_one("alice", "vault2", "b.md", {"modified_at_ms": 1, "size_bytes": 200, "content_hash": "h2", "is_deleted": False})
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    response = asyncio.run(servicer.GetSize(wire_types.SizeRequest(vault_id="vault1"), FakeContext()))

    assert isinstance(response, wire_types.SizeResponse)
    assert response.vault_size_bytes == 100
    assert response.total_size_bytes == 300
    assert response.limit_bytes == -1


def test_get_size_is_zero_for_a_brand_new_vault(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    response = asyncio.run(servicer.GetSize(wire_types.SizeRequest(vault_id="vault1"), FakeContext()))

    assert response.vault_size_bytes == 0
    assert response.total_size_bytes == 0


# ─── PurgeVault ─────────────────────────────────────────────────────────────────────────────


def test_purge_vault_deletes_files_history_backups_and_metadata(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("hello")
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # creates a metadata row + history backup
    history_rows = repo.get_history("alice", "vault1", "note.md")
    assert len(history_rows) == 1
    backup_path = history_rows[0]["backup_file_path"]
    assert os.path.exists(backup_path)  # sanity check before purge

    response = asyncio.run(servicer.PurgeVault(wire_types.PurgeRequest(vault_id="vault1"), FakeContext()))

    assert isinstance(response, wire_types.PurgeResponse)
    assert response.ok is True
    assert repo.load_all("alice", "vault1") == {}
    assert repo.get_history("alice", "vault1", "note.md") == []
    assert not vault_path.exists()
    assert not os.path.exists(backup_path)


def test_purge_vault_only_affects_the_calling_owner_and_vault(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    other_vault_path = tmp_path / "vaults" / "alice" / "vault2"
    other_vault_path.mkdir(parents=True)
    (other_vault_path / "keep.md").write_text("keep me")
    servicer._scan_and_merge(str(other_vault_path), "alice", "vault2")

    asyncio.run(servicer.PurgeVault(wire_types.PurgeRequest(vault_id="vault1"), FakeContext()))

    assert list(repo.load_all("alice", "vault2").keys()) == ["keep.md"]
    assert other_vault_path.exists()


def test_purge_vault_of_a_never_synced_vault_still_succeeds(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    response = asyncio.run(servicer.PurgeVault(wire_types.PurgeRequest(vault_id="never-synced"), FakeContext()))

    assert response.ok is True


# ─── GetUsernames ───────────────────────────────────────────────────────────────────────────


def test_get_usernames_returns_only_the_owner_when_no_shares_exist(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    response = asyncio.run(servicer.GetUsernames(wire_types.UsernamesRequest(vault_id="vault1"), FakeContext()))

    assert isinstance(response, wire_types.UsernamesResponse)
    assert response.usernames == ["alice"]


def test_get_usernames_includes_accepted_collaborators(tmp_path, repo):
    """See pumice's 14_vault_sharing_설계.md -- GetUsernames now lists the owner plus every
    account with an accepted vault-sharing invite."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    repo.create_user("bob", "hash", email="bob@example.com")
    repo.create_user("carol", "hash", email="carol@example.com")
    repo.add_vault_share("alice", "vault1", "bob@example.com")
    repo.add_vault_share("alice", "vault1", "carol@example.com")
    # Only accept bob's share -- carol's stays pending and must not be listed.
    repo.resolve_vault_owner("bob", "alice", "vault1", mark_accepted=True)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    response = asyncio.run(servicer.GetUsernames(wire_types.UsernamesRequest(vault_id="vault1"), FakeContext()))

    assert response.usernames == ["alice", "bob"]
