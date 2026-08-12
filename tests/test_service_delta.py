"""TDD for a real-world Delta failure:

    ValueError: Protocol message FileMeta has no "owner_username" field.

_scan_and_merge()'s "keep the existing deleted state" branch reuses a tombstone row exactly
as repository.load_all() returned it -- a full DB row (owner_username, vault_id, path,
modified_at_ms, size_bytes, content_hash, is_deleted), not the 5-key shape
(path/modified_at_ms/size_bytes/content_hash/is_deleted) every other entry in merged_files
has. Delta() later does `sync_pb2.FileMeta(**server_meta)` for a server-side tombstone the
client hasn't caught up to yet, which blows up on the first unexpected key.

This exact dict-shape mismatch already caused one other bug (see change_notifier.py's
did_metadata_change docstring) -- fixed there defensively at its own call site, but the root
cause in _scan_and_merge was left in place, so it kept surfacing at other call sites. Fixing
it at the source means every consumer of merged_files/server_files is safe, not just the ones
patched so far.

Reproduction needs three _scan_and_merge cycles against a real (sqlite) repository, because
the raw dict only appears on read *after* it's round-tripped through the DB once:
  1. File exists on disk -> scanned fresh, clean 5-key dict, persisted.
  2. File removed from disk -> "missing but not yet marked deleted" branch builds a clean
     5-key tombstone dict itself and persists it (this cycle is NOT buggy).
  3. File still absent -> repository.load_all() now returns *that* tombstone back as a full
     DB row (owner_username/vault_id included) -- "keep the existing deleted state" reuses it
     verbatim. This is the cycle that reproduces the bug.
"""

import asyncio

import pytest

from pumice_server import wire_types
from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer

CLEAN_KEYS = {"path", "modified_at_ms", "size_bytes", "content_hash", "is_deleted"}


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def make_servicer(tmp_path, repo):
    return SyncServiceServicer(data_dir=str(tmp_path), repository=repo)


def test_a_tombstone_kept_across_two_cycles_stays_a_clean_5_key_dict(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    note = vault_path / "note.md"
    note.write_text("hello")

    servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # cycle 1: fresh scan

    note.unlink()
    first_delete = servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # cycle 2: newly deleted
    assert set(first_delete["note.md"].keys()) == CLEAN_KEYS

    second_poll = servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # cycle 3: kept tombstone
    assert set(second_poll["note.md"].keys()) == CLEAN_KEYS

    # The actual regression: constructing the wire message from this dict must not raise.
    wire_types.FileMeta(**second_poll["note.md"])


class FakeContext:
    def __init__(self, token="tok"):
        self._token = token

    def invocation_metadata(self):
        return [("authorization", f"Bearer {self._token}")]

    async def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")


def test_delta_does_not_crash_when_client_has_not_caught_up_to_a_kept_tombstone(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    note = vault_path / "note.md"
    note.write_text("hello")

    servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    note.unlink()
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # deletion first detected+persisted

    # The client polls again believing it still has the file (hasn't synced the deletion yet).
    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="note.md", modified_at_ms=1, size_bytes=5,
                                          content_hash="deadbeef", is_deleted=False)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    # Delta() must speak wire_types now, not sync_pb2 -- PR5 (see pumice's 13_PR5_계획.md).
    assert isinstance(response, wire_types.DeltaResponse)
    assert all(isinstance(m, wire_types.FileMeta) for m in response.need_download)

    assert [f.path for f in response.need_download] == ["note.md"]
    assert response.need_download[0].is_deleted is True


def test_delta_request_and_response_are_plain_dataclasses_not_protobuf(tmp_path, repo):
    """Focused regression for the PR5 migration itself -- a minimal happy-path round trip
    (one client-only file needing upload, one server-only file needing download) asserting
    purely on the wire_types boundary, independent of the tombstone edge case above."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "server-only.md").write_text("server side")

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="client-only.md", modified_at_ms=1, size_bytes=4,
                                          content_hash="abc123", is_deleted=False)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert isinstance(response, wire_types.DeltaResponse)
    assert response.need_upload == ["client-only.md"]
    assert len(response.need_download) == 1
    downloaded = response.need_download[0]
    assert isinstance(downloaded, wire_types.FileMeta)
    assert downloaded.path == "server-only.md"
