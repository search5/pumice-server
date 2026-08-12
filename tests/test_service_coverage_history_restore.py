"""Coverage-focused tests for SyncServiceServicer's error/edge-case branches in
src/pumice_server/service.py that the happy-path suites (test_service_history.py,
test_service_download_history_version.py, test_service_download_files.py) don't reach:
GetFileHistory/DownloadHistoryVersion/RestoreHistoryVersion's abort/error paths, the
per-file ownership-check exception guard inside DownloadFiles's _stream_download_file, and
PurgeVault's two exception guards (a single backup file failing to delete vs. the whole RPC
failing).

Follows test_service_history.py's fixtures/FakeContext pattern (async `abort()` raises
RuntimeError -- both GetFileHistory and DownloadHistoryVersion `await context.abort(...)`,
so a raising coroutine is what actually propagates the abort) and borrows
test_service_download_files.py's FakeContext.write() bookkeeping for the DownloadFiles test.
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
        self.written = []

    def invocation_metadata(self):
        return [
            ("authorization", f"Bearer {self._token}"),
            ("x-device-name", "test-device"),
            ("x-user-name", "test-user"),
        ]

    async def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")

    async def write(self, message):
        self.written.append(message)


class RecordingChangeNotifier:
    def __init__(self):
        self.calls = []

    def notify_change(self, owner_username, vault_id, meta):
        self.calls.append((owner_username, vault_id, meta))


async def _collect(agen):
    return [x async for x in agen]


def make_servicer_with_one_history_entry(tmp_path, repo, change_notifier=None):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=change_notifier)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("v1")
    # First scan of a newly-discovered file creates a history backup (see _scan_and_merge).
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    return servicer, vault_path


def get_only_history_id(repo, owner_username, vault_id, path):
    rows = repo.get_history(owner_username, vault_id, path)
    assert len(rows) == 1
    return rows[0]["history_id"]


# --- DownloadFiles / _stream_download_file (line ~727-729): a per-file ownership/path-resolution
# failure is swallowed for that one file rather than raising out of the whole batch. -----------


def test_download_files_silently_skips_a_file_when_vault_path_resolution_fails(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    # No vault directory is even created: vault_id="../escape-vault" makes _get_vault_path
    # resolve to a path outside the caller's own owner_dir (vaults/alice), which raises
    # ValueError("Invalid vault ID") -- caught by _stream_download_file's except block, which
    # logs and returns instead of propagating.
    request = wire_types.DownloadBatchRequest(vault_id="../escape-vault", paths=["a.md"])
    context = FakeContext()

    # Must not raise.
    asyncio.run(servicer.DownloadFiles(request, context))

    assert context.written == []


# --- GetFileHistory (line ~805-807): unexpected exception during lookup aborts INTERNAL. -------


def test_get_file_history_aborts_with_internal_status_on_unexpected_repository_error(tmp_path, repo, monkeypatch):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)

    def raise_boom(owner_username, vault_id, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(repo, "get_history", raise_boom)

    request = wire_types.HistoryRequest(vault_id="vault1", path="note.md")
    with pytest.raises(RuntimeError, match="aborted: boom"):
        asyncio.run(servicer.GetFileHistory(request, FakeContext()))


# --- DownloadHistoryVersion (line ~816-822, ~856-858): not-found / missing-backup / unexpected
# error all abort the stream instead of yielding chunks. ----------------------------------------


def test_download_history_version_aborts_not_found_for_unknown_history_id(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)

    request = wire_types.HistoryVersionDownloadRequest(vault_id="vault1", path="note.md", history_id=999999)
    with pytest.raises(RuntimeError, match="aborted: History version not found"):
        asyncio.run(_collect(servicer.DownloadHistoryVersion(request, FakeContext())))


def test_download_history_version_aborts_not_found_when_backup_file_missing_on_disk(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")
    row = repo.get_history_by_id("alice", history_id)
    # The DB row survives, but its backup file on disk does not.
    os.remove(row["backup_file_path"])

    request = wire_types.HistoryVersionDownloadRequest(vault_id="vault1", path="note.md", history_id=history_id)
    with pytest.raises(RuntimeError, match="aborted: Backup file not found on disk"):
        asyncio.run(_collect(servicer.DownloadHistoryVersion(request, FakeContext())))


def test_download_history_version_aborts_internal_on_unexpected_repository_error(tmp_path, repo, monkeypatch):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    def raise_kaboom(owner_username, history_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(repo, "get_history_by_id", raise_kaboom)

    request = wire_types.HistoryVersionDownloadRequest(vault_id="vault1", path="note.md", history_id=1)
    with pytest.raises(RuntimeError, match="aborted: kaboom"):
        asyncio.run(_collect(servicer.DownloadHistoryVersion(request, FakeContext())))


# --- RestoreHistoryVersion (line ~870, ~881, ~908, ~929-936): error responses, the
# change_notifier call on success, and the non-fatal history-recording try/except. --------------


def test_restore_history_version_returns_error_when_backup_file_missing_on_disk(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")
    row = repo.get_history_by_id("alice", history_id)
    os.remove(row["backup_file_path"])

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is False
    assert response.error == "Backup file not found on disk"


def test_restore_history_version_rejects_an_absolute_target_path(tmp_path, repo):
    """An absolute request.path (e.g. "/etc/passwd") makes os.path.join(vault_path, path)
    discard vault_path entirely and return the absolute path verbatim, so the
    dest_file_path.startswith(vault_path) check correctly flags it as an escape attempt and
    the restore is refused before any file is touched."""
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="/etc/passwd", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is False
    assert response.error == "Invalid target path (escape attempt)"
    # Untouched: the note itself was never restored over, and the vault directory has nothing
    # else in it.
    assert (vault_path / "note.md").read_text() == "v1"


def test_restore_history_version_rejects_a_relative_dot_dot_escape(tmp_path, repo):
    """Regression test for a real path-traversal vulnerability this coverage effort found (see
    llm-wiki/10-*.md): the "escape attempt" guard used to prefix-check the *unresolved* joined
    path, so a relative ".." segment (e.g. "../escaped.md") produced a string that still
    literally started with vault_path (e.g. ".../vaults/alice/vault1/../escaped.md") and sailed
    through the check, only for os.makedirs/shutil.copy2 to resolve the ".." for real and write
    the restored file outside the vault directory. Fixed by resolving dest_file_path via
    os.path.abspath before the check -- confirm it's now rejected the same way an absolute
    escape path already was (see the sibling test above)."""
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="../escaped.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is False
    assert response.error == "Invalid target path (escape attempt)"
    # Nothing was written outside the vault, and note.md itself was left alone.
    assert not (vault_path.parent / "escaped.md").exists()
    assert (vault_path / "note.md").read_text() == "v1"


def test_restore_history_version_notifies_change_notifier_on_success(tmp_path, repo):
    change_notifier = RecordingChangeNotifier()
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo, change_notifier=change_notifier)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")
    # _scan_and_merge's own first-discovery notify (see change_notifier.py) already fired
    # during setup above; only the restore call's own notification is under test here.
    change_notifier.calls.clear()

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is True
    assert len(change_notifier.calls) == 1
    owner_username, vault_id, meta = change_notifier.calls[0]
    assert owner_username == "alice"
    assert vault_id == "vault1"
    assert meta["path"] == "note.md"
    assert meta["is_deleted"] is False


def test_restore_history_version_still_succeeds_when_recording_restore_history_fails(tmp_path, repo, monkeypatch):
    """The restore itself (file copy + metadata update) is already done and reported before
    this second, purely-informational history entry is recorded; the surrounding try/except
    (line ~929-930) is explicitly meant to make that recording failure non-fatal."""
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")

    def raise_on_add_history(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(repo, "add_history", raise_on_add_history)

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is True
    assert response.error == ""
    # The restore itself still physically happened.
    assert (vault_path / "note.md").read_text() == "v1"


def test_restore_history_version_returns_error_when_an_unexpected_exception_is_raised(tmp_path, repo, monkeypatch):
    """Covers RestoreHistoryVersion's outer try/except (line ~934-936), distinct from the
    inner non-fatal one above: a failure in the restore itself (here, persisting the updated
    metadata) is reported as ok=False with the exception's message, not swallowed."""
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")

    def raise_on_save_one(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "save_one", raise_on_save_one)

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok is False
    assert response.error == "disk full"


# --- PurgeVault (line ~962-963, ~971-973): one backup file failing to delete during purge is
# non-fatal, but the RPC as a whole reports failure if purge_vault() itself raises. -------------


def test_purge_vault_continues_when_a_backup_file_fails_to_delete(tmp_path, repo, monkeypatch):
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    history_id = get_only_history_id(repo, "alice", "vault1", "note.md")
    backup_file_path = repo.get_history_by_id("alice", history_id)["backup_file_path"]
    assert os.path.exists(backup_file_path)

    real_remove = os.remove

    def flaky_remove(path):
        if path == backup_file_path:
            raise OSError("simulated removal failure")
        real_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)

    request = wire_types.PurgeRequest(vault_id="vault1")
    response = asyncio.run(servicer.PurgeVault(request, FakeContext()))

    # The one backup file that failed to delete is logged and skipped, not fatal to the purge.
    assert response.ok is True
    assert response.error == ""
    # The DB rows and the vault directory itself were still purged.
    assert repo.get_history("alice", "vault1", "note.md") == []
    assert not vault_path.exists()


def test_purge_vault_returns_error_when_purge_vault_itself_raises(tmp_path, repo, monkeypatch):
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)

    def raise_boom(owner_username, vault_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(repo, "purge_vault", raise_boom)

    request = wire_types.PurgeRequest(vault_id="vault1")
    response = asyncio.run(servicer.PurgeVault(request, FakeContext()))

    assert response.ok is False
    assert response.error == "db unavailable"
    # Nothing was torn down since purge_vault() failed before any filesystem work happened.
    assert vault_path.exists()
