"""TDD for SyncServiceServicer.finalize_pushed_file/finalize_deduped_file -- PR6 of
#14_옵시디언싱크_정렬_구현계획.md (pumice repo): the per-file `push_req` upload path that
replaces the old batch-oriented UploadFiles/_finalize_uploaded_file for anything reachable over
WS (UploadFiles itself is left in place, unreachable/dead, pending a separate follow-up PR6b --
see the design doc).

Unlike _finalize_uploaded_file (which relied on a whole-vault metadata_cache/tombstones_by_hash
pre-loaded once per UploadFiles *batch*), these look up just the one path being finalized via
repository.load_one() -- there is no batch boundary to amortize a whole-vault load against
anymore now that every push is its own independent request. Tombstone-hash rename detection is
deliberately not carried over: confirmed dead code (every tombstone-creation site blanks
content_hash to "", so it could never have matched anything), not something this simplification
newly breaks.
"""

import hashlib
import os

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def make_servicer(tmp_path, repo, **kwargs):
    return SyncServiceServicer(data_dir=str(tmp_path), repository=repo, **kwargs)


# ─── finalize_pushed_file ────────────────────────────────────────────────────────────────────


def test_finalize_pushed_file_writes_the_content_and_succeeds(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"hello from push"
    content_hash = hashlib.sha256(content).hexdigest()

    ok, err = servicer.finalize_pushed_file(
        "alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user",
    )

    assert ok is True
    assert err == ""
    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert written.read_bytes() == content


def test_finalize_pushed_file_rejects_a_hash_mismatch_without_writing_the_file(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"tampered"

    ok, err = servicer.finalize_pushed_file(
        "alice", "vault1", "note.md", content, len(content), 1000, "0" * 64, "dev", "user",
    )

    assert ok is False
    assert "hash" in err.lower()
    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert not written.exists()
    assert repo.load_one("alice", "vault1", "note.md") is None


def test_finalize_pushed_file_rejects_a_size_mismatch(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"1234567890"
    content_hash = hashlib.sha256(content).hexdigest()

    ok, err = servicer.finalize_pushed_file(
        "alice", "vault1", "note.md", content, 3, 1000, content_hash, "dev", "user",
    )

    assert ok is False
    assert "size" in err.lower()


def test_finalize_pushed_file_updates_metadata_and_the_change_log(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"content"
    content_hash = hashlib.sha256(content).hexdigest()

    servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    meta = repo.load_one("alice", "vault1", "note.md")
    assert meta["content_hash"] == content_hash
    assert meta["size_bytes"] == len(content)
    assert meta["is_deleted"] is False
    changes = repo.get_changes_since("alice", "vault1", 0)
    assert [c["path"] for c in changes] == ["note.md"]


def test_finalize_pushed_file_records_a_history_backup_for_a_new_file(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"content"
    content_hash = hashlib.sha256(content).hexdigest()

    servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    history = repo.get_history("alice", "vault1", "note.md")
    assert len(history) == 1
    assert history[0]["content_hash"] == content_hash


def test_finalize_pushed_file_skips_a_duplicate_backup_for_an_identical_reupload(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"content"
    content_hash = hashlib.sha256(content).hexdigest()
    servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    # Re-uploading the exact same bytes (e.g. a client re-sync) shouldn't create a second
    # near-identical history entry -- mirrors _finalize_uploaded_file's existing should_backup
    # duplicate-hash check.
    servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 2000, content_hash, "dev", "user")

    history = repo.get_history("alice", "vault1", "note.md")
    assert len(history) == 1


def test_finalize_pushed_file_skips_history_for_an_excluded_config_path_but_still_updates_metadata(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"plugin code"
    content_hash = hashlib.sha256(content).hexdigest()

    ok, err = servicer.finalize_pushed_file(
        "alice", "vault1", ".obsidian/plugins/foo/main.js", content, len(content), 1000, content_hash, "dev", "user",
    )

    assert ok is True, err
    assert repo.get_history("alice", "vault1", ".obsidian/plugins/foo/main.js") == []
    meta = repo.load_one("alice", "vault1", ".obsidian/plugins/foo/main.js")
    assert meta["content_hash"] == content_hash
    changes = repo.get_changes_since("alice", "vault1", 0)
    assert [c["path"] for c in changes] == [".obsidian/plugins/foo/main.js"]


def test_finalize_pushed_file_notifies_the_change_notifier(tmp_path, repo):
    notified = []

    class FakeNotifier:
        def notify_change(self, owner, vault_id, meta):
            notified.append((owner, vault_id, meta))

    servicer = make_servicer(tmp_path, repo, change_notifier=FakeNotifier())
    content = b"content"
    content_hash = hashlib.sha256(content).hexdigest()

    servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    assert len(notified) == 1
    assert notified[0][2]["path"] == "note.md"


def test_finalize_pushed_file_swallows_a_backup_failure_and_still_succeeds(tmp_path, repo, monkeypatch):
    import pumice_server.service as service_module

    def failing_backup(src_path, dst_path):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(service_module, "_backup_file", failing_backup)
    servicer = make_servicer(tmp_path, repo)
    content = b"content that fails to back up"
    content_hash = hashlib.sha256(content).hexdigest()

    ok, err = servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    assert ok is True, err
    assert (tmp_path / "vaults" / "alice" / "vault1" / "note.md").read_bytes() == content
    assert repo.get_history("alice", "vault1", "note.md") == []


def test_finalize_pushed_file_acks_a_failure_and_cleans_up_the_temp_file_on_a_move_failure(tmp_path, repo, monkeypatch):
    real_rename = os.rename

    def failing_rename(src, dst):
        if str(dst).endswith("note.md"):
            raise OSError("simulated: rename failed")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing_rename)
    servicer = make_servicer(tmp_path, repo)
    content = b"this file's move will fail"
    content_hash = hashlib.sha256(content).hexdigest()

    ok, err = servicer.finalize_pushed_file("alice", "vault1", "note.md", content, len(content), 1000, content_hash, "dev", "user")

    assert ok is False
    assert "File move failed" in err
    assert not any((tmp_path / "vaults" / "alice" / "vault1").glob("*.tmp-*"))


# ─── finalize_deduped_file ───────────────────────────────────────────────────────────────────


def test_finalize_deduped_file_copies_the_source_content_to_the_new_path(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    content = b"shared content"
    content_hash = hashlib.sha256(content).hexdigest()
    servicer.finalize_pushed_file("alice", "vault1", "original.md", content, len(content), 1000, content_hash, "dev", "user")
    source_meta = repo.load_one("alice", "vault1", "original.md")

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    copied = tmp_path / "vaults" / "alice" / "vault1" / "copy.md"
    assert copied.read_bytes() == content
    original = tmp_path / "vaults" / "alice" / "vault1" / "original.md"
    assert original.read_bytes() == content  # source untouched


def test_finalize_deduped_file_records_metadata_matching_the_source_hash(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    source_meta = {"path": "original.md", "content_hash": "h1", "size_bytes": 5, "modified_at_ms": 1, "is_deleted": False}
    (tmp_path / "vaults" / "alice" / "vault1").mkdir(parents=True)
    (tmp_path / "vaults" / "alice" / "vault1" / "original.md").write_bytes(b"hello")
    repo.save_one("alice", "vault1", "original.md", source_meta)

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    meta = repo.load_one("alice", "vault1", "copy.md")
    assert meta["content_hash"] == "h1"
    assert meta["size_bytes"] == 5
    assert meta["modified_at_ms"] == 2000
    assert meta["is_deleted"] is False


def test_finalize_deduped_file_records_a_change_log_entry_and_a_history_backup(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    source_meta = {"path": "original.md", "content_hash": "h1", "size_bytes": 5, "modified_at_ms": 1, "is_deleted": False}
    (tmp_path / "vaults" / "alice" / "vault1").mkdir(parents=True)
    (tmp_path / "vaults" / "alice" / "vault1" / "original.md").write_bytes(b"hello")
    repo.save_one("alice", "vault1", "original.md", source_meta)

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    changes = repo.get_changes_since("alice", "vault1", 0)
    assert [c["path"] for c in changes] == ["copy.md"]
    history = repo.get_history("alice", "vault1", "copy.md")
    assert len(history) == 1
    assert history[0]["content_hash"] == "h1"


def test_finalize_deduped_file_notifies_the_change_notifier(tmp_path, repo):
    notified = []

    class FakeNotifier:
        def notify_change(self, owner, vault_id, meta):
            notified.append((owner, vault_id, meta))

    servicer = make_servicer(tmp_path, repo, change_notifier=FakeNotifier())
    source_meta = {"path": "original.md", "content_hash": "h1", "size_bytes": 5, "modified_at_ms": 1, "is_deleted": False}
    (tmp_path / "vaults" / "alice" / "vault1").mkdir(parents=True)
    (tmp_path / "vaults" / "alice" / "vault1" / "original.md").write_bytes(b"hello")
    repo.save_one("alice", "vault1", "original.md", source_meta)

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    assert len(notified) == 1
    assert notified[0][2]["path"] == "copy.md"


def test_finalize_deduped_file_overwrites_an_existing_file_at_the_destination_path(tmp_path, repo):
    # Same path re-pushed with content that happens to match some other live file's hash --
    # dedup still has to replace whatever was physically there before linking the match in.
    servicer = make_servicer(tmp_path, repo)
    source_meta = {"path": "original.md", "content_hash": "h1", "size_bytes": 5, "modified_at_ms": 1, "is_deleted": False}
    vault_dir = tmp_path / "vaults" / "alice" / "vault1"
    vault_dir.mkdir(parents=True)
    (vault_dir / "original.md").write_bytes(b"hello")
    (vault_dir / "copy.md").write_bytes(b"stale content that must be replaced")
    repo.save_one("alice", "vault1", "original.md", source_meta)

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    assert (vault_dir / "copy.md").read_bytes() == b"hello"


def test_finalize_deduped_file_swallows_a_backup_failure_and_still_succeeds(tmp_path, repo, monkeypatch):
    import pumice_server.service as service_module

    real_backup_file = service_module._backup_file

    def flaky_backup(src_path, dst_path):
        # Let the dedup link itself (source -> dest) through, only fail the history backup copy
        # (dest -> history dir) that comes after.
        if "history" in dst_path:
            raise OSError("simulated backup failure")
        return real_backup_file(src_path, dst_path)

    monkeypatch.setattr(service_module, "_backup_file", flaky_backup)
    servicer = make_servicer(tmp_path, repo)
    source_meta = {"path": "original.md", "content_hash": "h1", "size_bytes": 5, "modified_at_ms": 1, "is_deleted": False}
    vault_dir = tmp_path / "vaults" / "alice" / "vault1"
    vault_dir.mkdir(parents=True)
    (vault_dir / "original.md").write_bytes(b"hello")
    repo.save_one("alice", "vault1", "original.md", source_meta)

    servicer.finalize_deduped_file("alice", "vault1", "copy.md", source_meta, 2000, "dev", "user")

    assert (vault_dir / "copy.md").read_bytes() == b"hello"
    assert repo.get_history("alice", "vault1", "copy.md") == []
