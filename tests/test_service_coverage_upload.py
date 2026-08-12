"""Coverage-focused unit tests for SyncServiceServicer.UploadFiles' hand-rolled streaming state
machine (src/pumice_server/service.py) and its finalization helper _finalize_uploaded_file.

tests/test_service_upload_files.py already covers the happy path plus the max-file-size and
header/eof content-hash-mismatch rejections. This file targets what's left uncovered in that
same method: the per-chunk state machine's error/edge branches (unauthorized header, invalid
path, orphaned data/eof chunks with no open file handle, path mismatches mid-stream, an
abruptly-ended stream, a superseded unfinished file) and _finalize_uploaded_file's bookkeeping
branches (history-excluded paths, rename-via-tombstone detection, duplicate-hash backup
dedup/force-backup, overwriting an existing file, a swallowed backup failure, change-notifier
firing, and the file-move failure path).

UploadFiles is not an async-generator *consumer* -- despite the RPC being a stream, the method
itself does a plain `for chunk in request.chunks` over wire_types.UploadBatch.chunks (a plain
list), while *yielding* one wire_types.UploadAck per finished (or rejected) file. So driving it
is just: build a UploadBatch(chunks=[...]) up front and collect the async-generator output,
exactly as test_service_upload_files.py does.
"""

import asyncio
import hashlib
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
    """Deliberately has no abort() method, matching test_service_upload_files.py's FakeContext.
    _verify_vault_access() calls context.abort() on an unrecognized token; since this double
    doesn't implement it, that call raises AttributeError, which UploadFiles' header branch
    catches as a generic auth failure -- convenient for exercising that except clause without
    needing to hand-implement gRPC's actual abort-raises-a-special-exception semantics."""

    def __init__(self, token="tok", device_name="test-device", user_name="test-user"):
        self._token = token
        self._device_name = device_name
        self._user_name = user_name

    def invocation_metadata(self):
        return [
            ("authorization", f"Bearer {self._token}"),
            ("x-device-name", self._device_name),
            ("x-user-name", self._user_name),
        ]


class FakeChangeNotifier:
    def __init__(self):
        self.calls = []

    def notify_change(self, owner_username, vault_id, meta):
        self.calls.append((owner_username, vault_id, meta))


async def _collect(agen):
    return [x async for x in agen]


def _upload(servicer, chunks, context=None):
    request = wire_types.UploadBatch(chunks=chunks)
    return asyncio.run(_collect(servicer.UploadFiles(request, context or FakeContext())))


def _header(vault_id="vault1", path="note.md", total_bytes=0, modified_at_ms=1000, content_hash=""):
    return wire_types.FileChunk(header=wire_types.ChunkHeader(
        vault_id=vault_id, path=path, total_bytes=total_bytes, modified_at_ms=modified_at_ms,
        content_hash=content_hash))


def _data(path="note.md", sequence=0, data=b""):
    return wire_types.FileChunk(data=wire_types.ChunkData(path=path, sequence=sequence, data=data))


def _eof(path="note.md", content_hash=""):
    return wire_types.FileChunk(eof=wire_types.ChunkEOF(path=path, content_hash=content_hash))


def _full_upload_chunks(path, content, vault_id="vault1", modified_at_ms=1000):
    content_hash = hashlib.sha256(content).hexdigest()
    return [
        _header(vault_id=vault_id, path=path, total_bytes=len(content), modified_at_ms=modified_at_ms),
        _data(path=path, sequence=0, data=content),
        _eof(path=path, content_hash=content_hash),
    ], content_hash


# ---------------------------------------------------------------------------
# Header-handling branches
# ---------------------------------------------------------------------------

def test_header_with_unrecognized_token_is_acked_as_auth_failure(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [_header(path="note.md")], context=FakeContext(token="not-a-real-token"))

    assert len(acks) == 1
    assert acks[0].ok is False
    assert acks[0].error == "Authorization failed"
    # Nothing was ever written for the rejected file.
    assert not (tmp_path / "vaults").exists() or not list((tmp_path / "vaults").rglob("note.md"))


@pytest.mark.parametrize("bad_path", ["../escape.md", "/etc/passwd"])
def test_header_with_path_traversal_or_absolute_path_is_rejected(tmp_path, repo, bad_path):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [_header(path=bad_path)])

    assert len(acks) == 1
    assert acks[0].ok is False
    assert acks[0].error == "Invalid file path"
    assert not (tmp_path / "tmp").exists()


def test_temp_file_creation_failure_is_acked_and_does_not_crash_the_stream(tmp_path, repo, monkeypatch):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("simulated: cannot open temp file")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)

    acks = _upload(servicer, [_header(path="note.md", total_bytes=5)])

    assert len(acks) == 1
    assert acks[0].ok is False
    assert "Failed to create temp file" in acks[0].error


def test_a_second_header_without_finishing_the_first_cleans_up_the_orphaned_temp_file(tmp_path, repo):
    """No eof (or even data) is ever sent for fileA before fileB's header arrives -- the state
    machine must close fileA's dangling file handle and delete its orphaned temp file rather
    than leaking it, and must still process fileB normally afterwards."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content_b = b"file B content"
    content_hash_b = hashlib.sha256(content_b).hexdigest()

    acks = _upload(servicer, [
        _header(path="A.md", total_bytes=100),  # never finished
        _header(path="B.md", total_bytes=len(content_b)),
        _data(path="B.md", data=content_b),
        _eof(path="B.md", content_hash=content_hash_b),
    ])

    # Only B produced an ack; A was silently superseded/cleaned up, never acked.
    assert len(acks) == 1
    assert acks[0].path == "B.md"
    assert acks[0].ok is True

    assert not (tmp_path / "vaults" / "alice" / "vault1" / "A.md").exists()
    assert (tmp_path / "vaults" / "alice" / "vault1" / "B.md").read_bytes() == content_b
    # A's orphaned temp file was removed, B's temp file was renamed away -- nothing left behind.
    assert list((tmp_path / "tmp").rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Data/eof chunks arriving with no open file handle, or for the wrong path
# ---------------------------------------------------------------------------

def test_data_chunk_with_no_open_file_handle_is_ignored(tmp_path, repo):
    """A data chunk with no preceding header (or arriving after the handle was already closed)
    must be dropped with a warning, not raise -- and must not itself produce an ack."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [_data(path="ghost.md", data=b"orphan bytes")])

    assert acks == []


def test_eof_chunk_with_no_open_file_handle_is_ignored(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [_eof(path="ghost.md", content_hash="deadbeef")])

    assert acks == []


def test_data_chunk_path_mismatch_closes_handle_and_acks_failure(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [
        _header(path="note.md", total_bytes=5),
        _data(path="different.md", data=b"hello"),
    ])

    assert len(acks) == 1
    assert acks[0].ok is False
    assert acks[0].error == "Path mismatch in data chunk"
    assert list((tmp_path / "tmp").rglob("*.tmp")) == []


def test_eof_chunk_path_mismatch_closes_handle_and_acks_failure(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"hello"

    acks = _upload(servicer, [
        _header(path="note.md", total_bytes=len(content)),
        _data(path="note.md", data=content),
        _eof(path="different.md", content_hash=hashlib.sha256(content).hexdigest()),
    ])

    assert len(acks) == 1
    assert acks[0].ok is False
    assert acks[0].error == "Path mismatch in eof chunk"
    assert list((tmp_path / "tmp").rglob("*.tmp")) == []


def test_stream_ending_without_an_eof_leaves_no_partial_file_or_temp_file_behind(tmp_path, repo):
    """The chunk stream simply ends (client crash / connection drop) right after a data chunk,
    with no eof ever arriving. The method's `finally` block must still close and discard the
    dangling temp file -- no ack is produced for a file that was never finished."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    acks = _upload(servicer, [
        _header(path="note.md", total_bytes=5),
        _data(path="note.md", data=b"hello"),
    ])

    assert acks == []
    assert not (tmp_path / "vaults" / "alice" / "vault1" / "note.md").exists()
    assert list((tmp_path / "tmp").rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# _finalize_uploaded_file bookkeeping branches
# ---------------------------------------------------------------------------

def test_history_excluded_path_is_stored_but_never_backed_up(tmp_path, repo):
    """.obsidian/plugins/** content is machine-managed and explicitly excluded from history by
    is_history_excluded_path() -- it should still be written and acked ok, just with no backup
    or history row created for it."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b'{"enabled": true}'
    path = ".obsidian/plugins/some-plugin/data.json"
    chunks, _ = _full_upload_chunks(path, content)

    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is True
    assert (tmp_path / "vaults" / "alice" / "vault1" / path).read_bytes() == content
    assert repo.get_history("alice", "vault1", path) == []


def test_uploading_a_new_file_whose_hash_matches_a_deletion_tombstone_is_recorded_as_a_rename(tmp_path, repo):
    """If some other path was previously deleted (a tombstone row) and a brand-new file shows up
    with that exact same content hash, _finalize_uploaded_file treats it as a rename: it moves
    the old path's history over to the new path and records the rename itself as a history entry
    with related_path set."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"renamed file content"
    content_hash = hashlib.sha256(content).hexdigest()
    old_path = "old_name.md"
    new_path = "new_name.md"

    # Seed a deletion tombstone for old_path with the same content hash the new upload will have.
    repo.save_one("alice", "vault1", old_path, {
        "path": old_path, "modified_at_ms": 500, "size_bytes": len(content),
        "content_hash": content_hash, "is_deleted": True,
    })
    repo.add_history(
        owner_username="alice", vault_id="vault1", path=old_path, modified_at_ms=500,
        size_bytes=len(content), content_hash=content_hash, backup_file_path="/fake/old.bak",
        device_name="dev", user_name="user",
    )

    chunks, _ = _full_upload_chunks(new_path, content)
    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is True

    new_history = repo.get_history("alice", "vault1", new_path)
    # The pre-existing history row was migrated to new_path, plus a fresh row records the rename
    # event itself (with related_path pointing back at the old path).
    assert len(new_history) == 2
    assert any(row["related_path"] == old_path for row in new_history)
    # Nothing is left behind under the old path.
    assert repo.get_history("alice", "vault1", old_path) == []


def test_rename_detection_failure_is_swallowed_and_the_upload_still_succeeds_without_a_rename_link(tmp_path, repo, monkeypatch):
    """The inner try/except around migrate_history_on_rename() in the rename-detection branch:
    if migrating the old path's history over fails, that failure is logged and swallowed rather
    than failing the whole upload -- the new file still gets written and acked ok, it just isn't
    linked back to the old path (no related_path, and the old tombstone/cache entries are left
    untouched since the pop() calls live after the failing call in the same try block)."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"renamed file content, migration will fail"
    content_hash = hashlib.sha256(content).hexdigest()
    old_path = "old_name.md"
    new_path = "new_name.md"

    repo.save_one("alice", "vault1", old_path, {
        "path": old_path, "modified_at_ms": 500, "size_bytes": len(content),
        "content_hash": content_hash, "is_deleted": True,
    })

    def failing_migrate(*args, **kwargs):
        raise RuntimeError("simulated: migrate_history_on_rename failed")

    monkeypatch.setattr(repo, "migrate_history_on_rename", failing_migrate)

    chunks, _ = _full_upload_chunks(new_path, content)
    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is True
    new_history = repo.get_history("alice", "vault1", new_path)
    assert len(new_history) == 1
    assert new_history[0]["related_path"] is None


def test_duplicate_hash_check_failure_is_swallowed_and_defaults_to_backing_up(tmp_path, repo, monkeypatch):
    """The inner try/except around the 'is this a duplicate re-upload' hash/history check: if
    checking existing history raises, it's logged and swallowed -- should_backup is left at its
    initial True, so the upload still proceeds (and, incidentally, still backs up) rather than
    failing outright."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"unchanged content, history check will fail on re-upload"

    first_chunks, _ = _full_upload_chunks("note.md", content)
    first_acks = _upload(servicer, first_chunks)
    assert first_acks[0].ok is True
    assert len(repo.get_history("alice", "vault1", "note.md")) == 1

    real_get_history = repo.get_history
    call_count = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated: get_history failed")
        return real_get_history(*args, **kwargs)

    monkeypatch.setattr(repo, "get_history", raise_on_first_call)

    second_chunks, _ = _full_upload_chunks("note.md", content)
    second_acks = _upload(servicer, second_chunks)

    assert second_acks[0].ok is True
    # The duplicate-check blew up, so should_backup stayed at its default True instead of being
    # skipped -- a second (redundant) history row got recorded rather than the upload failing.
    assert len(repo.get_history("alice", "vault1", "note.md")) == 2


def test_reuploading_identical_content_with_no_prior_history_still_forces_one_backup(tmp_path, repo):
    """Covers the 'existing file, same hash, but zero history rows yet' edge case (e.g. metadata
    was seeded directly rather than via a prior UploadFiles call) -- _finalize_uploaded_file
    deliberately forces a backup here ('First history for X - force backup') instead of treating
    it as a no-op dedup skip, since skipping would otherwise mean the file could end up with no
    history at all."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"same content twice"
    content_hash = hashlib.sha256(content).hexdigest()

    vault_dir = tmp_path / "vaults" / "alice" / "vault1"
    vault_dir.mkdir(parents=True)
    (vault_dir / "note.md").write_bytes(content)
    repo.save_one("alice", "vault1", "note.md", {
        "path": "note.md", "modified_at_ms": 900, "size_bytes": len(content),
        "content_hash": content_hash, "is_deleted": False,
    })
    assert repo.get_history("alice", "vault1", "note.md") == []  # sanity: no history yet

    chunks, _ = _full_upload_chunks("note.md", content)
    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is True
    assert len(repo.get_history("alice", "vault1", "note.md")) == 1


def test_reuploading_identical_content_with_existing_history_skips_the_duplicate_backup(tmp_path, repo):
    """Once at least one history row already exists for a path, re-uploading byte-identical
    content again is a pure no-op dedup: no new backup/history row is created."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"unchanged content"
    chunks, _ = _full_upload_chunks("note.md", content)

    first_acks = _upload(servicer, chunks)
    assert first_acks[0].ok is True
    assert len(repo.get_history("alice", "vault1", "note.md")) == 1

    # Re-upload the exact same bytes again as a fresh batch.
    chunks_again, _ = _full_upload_chunks("note.md", content)
    second_acks = _upload(servicer, chunks_again)

    assert second_acks[0].ok is True
    # Still exactly one history row -- the second, identical upload added nothing.
    assert len(repo.get_history("alice", "vault1", "note.md")) == 1


def test_overwriting_an_existing_file_with_different_content_replaces_it_on_disk(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    first_chunks, _ = _full_upload_chunks("note.md", b"version one")
    first_acks = _upload(servicer, first_chunks)
    assert first_acks[0].ok is True

    second_chunks, _ = _full_upload_chunks("note.md", b"version two, much longer than before")
    second_acks = _upload(servicer, second_chunks)
    assert second_acks[0].ok is True

    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert written.read_bytes() == b"version two, much longer than before"
    # Both versions got their own history/backup entry.
    assert len(repo.get_history("alice", "vault1", "note.md")) == 2


def test_a_backup_failure_is_swallowed_and_does_not_fail_the_upload(tmp_path, repo, monkeypatch):
    """The comment above the backup try/except in _finalize_uploaded_file is explicit: a
    filesystem backup failure must never block the metadata update that makes the upload count
    as successful. It should just silently skip recording that backup (no history row)."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    import pumice_server.service as service_module

    def failing_backup(src_path, dst_path):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(service_module, "_backup_file", failing_backup)

    content = b"content that fails to back up"
    chunks, _ = _full_upload_chunks("note.md", content)
    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is True
    assert acks[0].error == ""
    # The file itself was still written successfully...
    assert (tmp_path / "vaults" / "alice" / "vault1" / "note.md").read_bytes() == content
    # ...but no history row was recorded, since the backup that would back it never happened.
    assert repo.get_history("alice", "vault1", "note.md") == []


def test_successful_upload_fires_the_change_notifier_with_the_new_files_metadata(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    notifier = FakeChangeNotifier()
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=notifier)
    content = b"notify me"
    chunks, content_hash = _full_upload_chunks("note.md", content, modified_at_ms=4242)

    acks = _upload(servicer, chunks)

    assert acks[0].ok is True
    assert len(notifier.calls) == 1
    owner_username, vault_id, meta = notifier.calls[0]
    assert owner_username == "alice"
    assert vault_id == "vault1"
    assert meta == {
        "path": "note.md",
        "modified_at_ms": 4242,
        "size_bytes": len(content),
        "content_hash": content_hash,
        "is_deleted": False,
    }


def test_a_file_move_failure_is_acked_as_a_failure_and_cleans_up_the_temp_file(tmp_path, repo, monkeypatch):
    """Forces _finalize_uploaded_file's outer except Exception branch: os.rename() (moving the
    validated temp file into place) fails. The upload must be acked as a failure rather than
    raising out of the RPC, and the now-useless temp file must be deleted rather than leaked."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    real_rename = os.rename

    def failing_rename(src, dst):
        if str(dst).endswith("note.md"):
            raise OSError("simulated: rename failed")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing_rename)

    content = b"this file's move will fail"
    chunks, _ = _full_upload_chunks("note.md", content)
    acks = _upload(servicer, chunks)

    assert len(acks) == 1
    assert acks[0].ok is False
    assert "File move failed" in acks[0].error
    assert not (tmp_path / "vaults" / "alice" / "vault1" / "note.md").exists()
    # The orphaned temp file was cleaned up rather than left behind.
    assert list((tmp_path / "tmp").rglob("*.tmp")) == []
