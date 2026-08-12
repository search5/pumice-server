"""Coverage-focused tests for SyncServiceServicer's less-exercised branches: the _backup_file
hardlink-fallback helper, _verify_vault_access's auth-failure/invalid-vault-id paths,
_scan_and_merge's directory/file exclusion + error-handling branches, _delete_on_server's main
branches, and Delta()'s deletion/hash-comparison/ValueError branches.

Follows the pattern established in test_service_delta.py / test_service_history.py: a fresh
in-memory SqlAlchemyMetadataRepository per test (the `repo` fixture), a FakeContext implementing
invocation_metadata()/abort(), and SyncServiceServicer constructed directly with
data_dir=str(tmp_path).

FakeContext.abort() here is a *plain* (non-async) method, unlike the async abort() used in
test_service_delta.py/test_service_history.py. That matters: _verify_vault_access calls
context.abort(...) synchronously (never awaited) -- calling an `async def abort` synchronously
just creates an unawaited coroutine and does NOT raise, so a test using an async abort would
silently observe _verify_vault_access falling through to `return ""` instead of the abort it's
meant to test. The real production context (ws_sync_resource.py's WsServicerContext.abort) is
itself a plain sync method that raises immediately, so a sync FakeContext.abort matches real
behavior -- and it still works fine at the `await context.abort(...)` call sites in Delta/
GetFileHistory/etc., since the raise happens while evaluating the call itself, before the await
ever gets a chance to await anything.
"""

import asyncio
import os

import pytest

from pumice_server import wire_types
from pumice_server import service as service_mod
from pumice_server.change_notifier import ChangeNotifier
from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer, _backup_file


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakeContext:
    def __init__(self, token="tok", metadata=None):
        self._token = token
        self._metadata = metadata

    def invocation_metadata(self):
        if self._metadata is not None:
            return self._metadata
        return [
            ("authorization", f"Bearer {self._token}"),
            ("x-device-name", "test-device"),
            ("x-user-name", "test-user"),
        ]

    def abort(self, code, details):
        raise RuntimeError(f"aborted[{code}]: {details}")

    async def write(self, message) -> None:  # pragma: no cover - unused by these tests
        raise NotImplementedError


def make_servicer(tmp_path, repo, **kwargs):
    return SyncServiceServicer(data_dir=str(tmp_path), repository=repo, **kwargs)


# ─── _backup_file (lines 34-35) ──────────────────────────────────────────────────────────────

def test_backup_file_falls_back_to_copy_when_hardlink_fails(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    src.write_text("hello world")
    dst = tmp_path / "dst.txt"

    def raising_link(*args, **kwargs):
        raise OSError("cross-device link not permitted")

    monkeypatch.setattr(os, "link", raising_link)

    _backup_file(str(src), str(dst))

    assert dst.exists()
    assert dst.read_text() == "hello world"
    # A real copy (not a hardlink) was made -- distinct inodes.
    assert os.stat(str(src)).st_ino != os.stat(str(dst)).st_ino


# ─── _verify_vault_access (lines 74-94) ──────────────────────────────────────────────────────

def test_verify_vault_access_aborts_when_authorization_header_missing(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    context = FakeContext(metadata=[])

    with pytest.raises(RuntimeError, match="Missing authorization metadata"):
        servicer._verify_vault_access("vault1", context)


def test_verify_vault_access_aborts_when_token_invalid(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    context = FakeContext(token="does-not-exist")

    with pytest.raises(RuntimeError, match="Invalid or missing auth token"):
        servicer._verify_vault_access("vault1", context)


def test_verify_vault_access_accepts_token_without_bearer_prefix(tmp_path, repo):
    repo.create_device_token("raw-token", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    context = FakeContext(metadata=[("authorization", "raw-token")])

    owner_username = servicer._verify_vault_access("vault1", context)

    assert owner_username == "alice"


def test_verify_vault_access_returns_owner_username_for_valid_bearer_token(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    context = FakeContext(token="tok")

    assert servicer._verify_vault_access("vault1", context) == "alice"


def test_get_vault_path_rejects_path_traversal_vault_id(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)

    with pytest.raises(ValueError, match="Invalid vault ID"):
        servicer._get_vault_path("alice", "../../etc/somewhere")


def test_delta_aborts_invalid_argument_for_path_traversal_vault_id(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    request = wire_types.DeltaRequest(vault_id="../../etc/somewhere", local_files=[])
    context = FakeContext()

    with pytest.raises(RuntimeError, match="Invalid vault ID"):
        asyncio.run(servicer.Delta(request, context))


# ─── _scan_and_merge (lines 112-219) ─────────────────────────────────────────────────────────

def test_scan_and_merge_excludes_nested_special_directories(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    (vault_path / ".trash").mkdir(parents=True)
    (vault_path / ".trash" / "deleted_note.md").write_text("gone")
    (vault_path / ".obsidian" / "cache").mkdir(parents=True)
    (vault_path / ".obsidian" / "cache" / "cachefile").write_text("cache")
    (vault_path / ".obsidian" / "plugins" / "pumice").mkdir(parents=True)
    (vault_path / ".obsidian" / "plugins" / "pumice" / "data.json").write_text("{}")
    (vault_path / ".git").mkdir(parents=True)
    (vault_path / ".git" / "config").write_text("config")
    (vault_path / "real_note.md").write_text("kept")

    merged = servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    assert set(merged.keys()) == {"real_note.md"}


def test_scan_and_merge_skips_excluded_top_level_files(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    (vault_path / ".obsidian").mkdir(parents=True)
    (vault_path / ".obsidian" / "workspace").write_text("ws")
    (vault_path / ".obsidian" / "workspace.json").write_text("{}")
    (vault_path / ".obsidian" / "workspace-mobile.json").write_text("{}")
    (vault_path / "._AppleDouble.md").write_text("meta")
    (vault_path / "real_note.md").write_text("kept")

    merged = servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    assert set(merged.keys()) == {"real_note.md"}


def test_scan_and_merge_reuses_cached_hash_for_unchanged_file(tmp_path, repo, monkeypatch):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("v1")

    calls = []
    original = service_mod.calculate_sha256

    def tracking(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(service_mod, "calculate_sha256", tracking)

    first = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    assert len(calls) == 1

    second = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    # File untouched -> same mtime/size -> cached content_hash reused, no rehash.
    assert len(calls) == 1
    assert second["note.md"]["content_hash"] == first["note.md"]["content_hash"]


def test_scan_and_merge_skips_file_and_logs_warning_when_hashing_fails(tmp_path, repo, monkeypatch):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    bad_file = vault_path / "broken.md"
    bad_file.write_text("v1")
    (vault_path / "good.md").write_text("fine")

    original = service_mod.calculate_sha256

    def selective_raise(path):
        if path.endswith("broken.md"):
            raise OSError("simulated read failure")
        return original(path)

    monkeypatch.setattr(service_mod, "calculate_sha256", selective_raise)

    merged = servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    # The failing file never made it into current_files (exception raised before that
    # assignment) and had no prior metadata to fall back on, so it's simply absent.
    assert "broken.md" not in merged
    assert "good.md" in merged


def test_scan_and_merge_continues_when_first_scan_backup_fails(tmp_path, repo, monkeypatch):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("v1")

    def raising_backup(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(service_mod, "_backup_file", raising_backup)

    merged = servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    # The backup failure is swallowed -- the file's metadata is still recorded normally.
    assert "note.md" in merged
    assert merged["note.md"]["is_deleted"] is False
    # ... but since _backup_file raised before add_history was reached, no history row exists.
    assert repo.get_history("alice", "vault1", "note.md") == []


def test_scan_and_merge_notifies_change_listener_for_new_file(tmp_path, repo):
    notifier = ChangeNotifier()
    received = []
    notifier.subscribe("alice", "vault1", lambda meta: received.append(meta))
    servicer = make_servicer(tmp_path, repo, change_notifier=notifier)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("v1")

    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    assert len(received) == 1
    assert received[0]["path"] == "note.md"
    assert received[0]["is_deleted"] is False


# ─── _delete_on_server (lines 223-290) ───────────────────────────────────────────────────────

def test_delete_on_server_removes_existing_file_and_records_tombstone_and_history(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    file_path = vault_path / "note.md"
    file_path.write_text("content")

    old_meta = {"modified_at_ms": 1000, "size_bytes": 7, "content_hash": "abc123"}

    meta = servicer._delete_on_server(
        str(vault_path), "alice", "vault1", "note.md", old_meta,
        c_time=1000, s_time=500, device_name="dev1", user_name="user1",
    )

    assert not file_path.exists()
    assert meta["is_deleted"] is True
    assert meta["modified_at_ms"] == 1001  # max(c_time, s_time) + 1
    assert meta["path"] == "note.md"

    stored = repo.load_one("alice", "vault1", "note.md")
    assert stored["is_deleted"] is True
    assert stored["modified_at_ms"] == 1001

    history_rows = repo.get_history("alice", "vault1", "note.md")
    assert len(history_rows) == 2  # last version + deletion marker
    deletion_rows = [r for r in history_rows if r.get("deleted")]
    assert len(deletion_rows) == 1
    assert deletion_rows[0]["content_hash"] == ""


def test_delete_on_server_without_old_meta_falls_back_to_stat_and_hash(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    file_path = vault_path / "note.md"
    file_path.write_text("content")
    expected_hash = service_mod.calculate_sha256(str(file_path))

    meta = servicer._delete_on_server(
        str(vault_path), "alice", "vault1", "note.md", None,
        c_time=2000, s_time=100, device_name="dev1", user_name="user1",
    )

    assert not file_path.exists()
    assert meta["modified_at_ms"] == 2001  # max(c_time, s_time) + 1
    history_rows = repo.get_history("alice", "vault1", "note.md")
    version_row = next(r for r in history_rows if not r.get("deleted"))
    # Fell back to stat()/calculate_sha256() (old_meta is None) rather than crashing.
    assert version_row["content_hash"] == expected_hash
    assert version_row["size_bytes"] == len("content")


def test_delete_on_server_still_deletes_file_when_backup_fails(tmp_path, repo, monkeypatch):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    file_path = vault_path / "note.md"
    file_path.write_text("content")

    def raising_backup(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(service_mod, "_backup_file", raising_backup)

    meta = servicer._delete_on_server(
        str(vault_path), "alice", "vault1", "note.md", None,
        c_time=10, s_time=20, device_name="dev1", user_name="user1",
    )

    # The backup failure (caught by its own inner try/except) never blocks the actual delete
    # or the tombstone that follows.
    assert not file_path.exists()
    assert meta["is_deleted"] is True
    stored = repo.load_one("alice", "vault1", "note.md")
    assert stored["is_deleted"] is True
    # No history rows since the backup step raised before add_history ran.
    assert repo.get_history("alice", "vault1", "note.md") == []


def test_delete_on_server_still_records_tombstone_when_os_remove_fails(tmp_path, repo, monkeypatch):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    file_path = vault_path / "note.md"
    file_path.write_text("content")

    original_remove = os.remove

    def raising_remove(path, *args, **kwargs):
        if os.path.normpath(path) == os.path.normpath(str(file_path)):
            raise OSError("permission denied")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", raising_remove)

    meta = servicer._delete_on_server(
        str(vault_path), "alice", "vault1", "note.md", None,
        c_time=10, s_time=20, device_name="dev1", user_name="user1",
    )

    # os.remove() failing (outer except at 278-279) still leaves the physical file in place...
    assert file_path.exists()
    # ...but the tombstone metadata is recorded regardless (it's outside the try/except).
    assert meta["is_deleted"] is True
    stored = repo.load_one("alice", "vault1", "note.md")
    assert stored["is_deleted"] is True
    # The backup itself succeeded before the (mocked) os.remove failure, so history exists.
    history_rows = repo.get_history("alice", "vault1", "note.md")
    assert len(history_rows) == 2


def test_delete_on_server_when_file_missing_on_disk_still_records_tombstone(tmp_path, repo):
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    # note.md never existed on disk.

    meta = servicer._delete_on_server(
        str(vault_path), "alice", "vault1", "note.md", None,
        c_time=10, s_time=20, device_name="dev1", user_name="user1",
    )

    assert meta["is_deleted"] is True
    assert meta["modified_at_ms"] == 21  # max(c_time, s_time) + 1
    stored = repo.load_one("alice", "vault1", "note.md")
    assert stored["is_deleted"] is True
    # No backup/version history since there was never a physical file to back up.
    assert repo.get_history("alice", "vault1", "note.md") == []


# ─── Delta() (lines 292-390) ─────────────────────────────────────────────────────────────────

def _setup_vault_with_file(tmp_path, repo, content="server content"):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text(content)
    return servicer, vault_path


def test_delta_client_deletion_removes_server_file_and_updates_tombstone(tmp_path, repo):
    servicer, vault_path = _setup_vault_with_file(tmp_path, repo)
    # Establish the server-side metadata for note.md first.
    server_before = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    s_time = server_before["note.md"]["modified_at_ms"]

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="note.md", modified_at_ms=s_time + 5000,
                                          size_bytes=0, content_hash="", is_deleted=True)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert not (vault_path / "note.md").exists()
    assert response.need_upload == []
    assert response.need_download == []
    stored = repo.load_one("alice", "vault1", "note.md")
    assert stored["is_deleted"] is True

    history_rows = repo.get_history("alice", "vault1", "note.md")
    assert any(r["device_name"] == "test-device" for r in history_rows)


def test_delta_ignores_client_side_deletion_of_file_server_never_had(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = make_servicer(tmp_path, repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="ghost.md", modified_at_ms=1, size_bytes=0,
                                          content_hash="", is_deleted=True)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert response.need_upload == []
    assert response.need_download == []


def test_delta_ignores_server_tombstone_the_client_never_knew_about(tmp_path, repo):
    servicer, vault_path = _setup_vault_with_file(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # cycle 1: fresh scan
    (vault_path / "note.md").unlink()
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")  # cycle 2: tombstone created

    request = wire_types.DeltaRequest(vault_id="vault1", local_files=[])  # client never had it
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert response.need_upload == []
    assert response.need_download == []


def test_delta_uploads_when_client_is_newer_and_hashes_differ(tmp_path, repo):
    servicer, vault_path = _setup_vault_with_file(tmp_path, repo)
    server_before = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    s_time = server_before["note.md"]["modified_at_ms"]

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="note.md", modified_at_ms=s_time + 100000,
                                          size_bytes=5, content_hash="clienthashdiffers",
                                          is_deleted=False)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert response.need_upload == ["note.md"]
    assert response.need_download == []


def test_delta_downloads_when_server_is_newer_and_hashes_differ(tmp_path, repo):
    servicer, vault_path = _setup_vault_with_file(tmp_path, repo)
    server_before = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    s_time = server_before["note.md"]["modified_at_ms"]
    assert s_time > 200000  # sanity: real epoch-ms mtimes are far bigger than our offset below

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="note.md", modified_at_ms=s_time - 100000,
                                          size_bytes=5, content_hash="clienthashdiffers",
                                          is_deleted=False)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    assert response.need_upload == []
    assert len(response.need_download) == 1
    assert response.need_download[0].path == "note.md"
    assert isinstance(response.need_download[0], wire_types.FileMeta)


def test_delta_uploads_when_timestamps_tie_and_hashes_differ(tmp_path, repo):
    servicer, vault_path = _setup_vault_with_file(tmp_path, repo)
    server_before = servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    s_time = server_before["note.md"]["modified_at_ms"]

    request = wire_types.DeltaRequest(
        vault_id="vault1",
        local_files=[wire_types.FileMeta(path="note.md", modified_at_ms=s_time,
                                          size_bytes=5, content_hash="clienthashdiffers",
                                          is_deleted=False)],
    )
    context = FakeContext()

    response = asyncio.run(servicer.Delta(request, context))

    # Safety-pin default: equal timestamps -> upload the client's version.
    assert response.need_upload == ["note.md"]
    assert response.need_download == []
