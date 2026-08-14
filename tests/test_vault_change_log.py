"""TDD for `vault_change_log` -- PR1 of #14_옵시디언싱크_정렬_구현계획.md (pumice repo).

A new, append-only, per-mutation journal that lets a client with a `lastKnownChangeId` baseline
catch up on exactly what changed since then, instead of requiring a full-vault Delta scan every
time (see the design doc's server data model investigation for why `file_history` -- append-only
too, but path-mutable on rename and paired with a physical backup copy -- isn't reusable for
this). This PR is intentionally read-side-free: nothing queries `vault_change_log` yet (that's
PR2, the `init` handshake catch-up). Here we only verify that every place that mutates
`file_metadata` also writes a corresponding journal row, and that `PurgeVault` -- which wipes
`file_metadata` outright -- leaves tombstones behind so a stale client isn't told "nothing
changed" for a vault that in fact just got emptied.
"""

import pytest
from sqlalchemy import select

from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def all_changes(repo, owner_username="alice", vault_id="vault-1"):
    with repo.engine.connect() as conn:
        rows = conn.execute(
            select(repo._vault_change_log)
            .where(
                repo._vault_change_log.c.owner_username == owner_username,
                repo._vault_change_log.c.vault_id == vault_id,
            )
            .order_by(repo._vault_change_log.c.change_id)
        ).fetchall()
        return [dict(r._mapping) for r in rows]


# ─── record_change (direct) ─────────────────────────────────────────────────────────────────


def test_record_change_inserts_a_row_with_the_given_fields(repo):
    meta = {"path": "a.md", "modified_at_ms": 1000, "size_bytes": 5, "content_hash": "h1", "is_deleted": False}

    repo.record_change("alice", "vault-1", "a.md", meta)

    [row] = all_changes(repo)
    assert row["path"] == "a.md"
    assert row["content_hash"] == "h1"
    assert row["size_bytes"] == 5
    assert row["modified_at_ms"] == 1000
    assert row["is_deleted"] is False
    assert isinstance(row["change_id"], int)
    assert row["recorded_at_ms"] > 0


def test_record_change_assigns_monotonically_increasing_change_ids(repo):
    meta_a = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "ha", "is_deleted": False}
    meta_b = {"path": "b.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "hb", "is_deleted": False}

    repo.record_change("alice", "vault-1", "a.md", meta_a)
    repo.record_change("alice", "vault-1", "b.md", meta_b)

    rows = all_changes(repo)
    assert [r["path"] for r in rows] == ["a.md", "b.md"]
    assert rows[1]["change_id"] > rows[0]["change_id"]


def test_record_change_is_scoped_by_owner_and_vault(repo):
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}

    repo.record_change("alice", "vault-1", "a.md", meta)
    repo.record_change("bob", "vault-1", "a.md", meta)
    repo.record_change("alice", "vault-2", "a.md", meta)

    assert len(all_changes(repo, "alice", "vault-1")) == 1
    assert len(all_changes(repo, "bob", "vault-1")) == 1
    assert len(all_changes(repo, "alice", "vault-2")) == 1


def test_record_change_does_not_deduplicate_repeated_mutations_of_the_same_path(repo):
    # Append-only: every mutation gets its own row, even if it's the same path changing twice in
    # a row -- a client resuming from a change_id between the two must still see both, not just
    # the latest (folding to "latest per path" happens on the read side in PR2, not here).
    meta_v1 = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False}
    meta_v2 = {"path": "a.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "h2", "is_deleted": False}

    repo.record_change("alice", "vault-1", "a.md", meta_v1)
    repo.record_change("alice", "vault-1", "a.md", meta_v2)

    rows = all_changes(repo)
    assert [r["content_hash"] for r in rows] == ["h1", "h2"]


# ─── record_change (batched) ────────────────────────────────────────────────────────────────


def test_batch_record_change_shares_the_batch_s_transaction(repo):
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}

    with repo.batch() as batch:
        batch.save_one("alice", "vault-1", "a.md", meta)
        batch.record_change("alice", "vault-1", "a.md", meta)

    assert repo.load_one("alice", "vault-1", "a.md")["content_hash"] == "h"
    [row] = all_changes(repo)
    assert row["content_hash"] == "h"


# ─── mutation call sites actually journal (service.py-level) ───────────────────────────────


def test_scan_and_merge_records_a_change_for_a_newly_discovered_file(tmp_path):
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    (vault_path / "a.md").write_text("hello")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    rows = all_changes(repo)
    assert [r["path"] for r in rows] == ["a.md"]
    assert rows[0]["is_deleted"] is False


def test_scan_and_merge_records_a_tombstone_for_a_file_removed_from_disk(tmp_path):
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    note = vault_path / "a.md"
    note.write_text("hello")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    note.unlink()
    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    rows = all_changes(repo)
    assert [r["path"] for r in rows] == ["a.md", "a.md"]
    assert rows[0]["is_deleted"] is False
    assert rows[1]["is_deleted"] is True


def test_scan_and_merge_does_not_record_a_change_for_an_untouched_file_on_a_later_poll(tmp_path):
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    (vault_path / "a.md").write_text("hello")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")  # no-op poll

    rows = all_changes(repo)
    assert len(rows) == 1  # only the first scan's discovery, not a second one for the re-poll


def test_scan_and_merge_records_config_dir_paths_excluded_from_file_history(tmp_path):
    # is_history_excluded_path() (.obsidian/plugins/**, .obsidian/bookmarks.json) still skips
    # file_history/backups, but the client still needs to learn these paths changed at all --
    # see #14's Non-goals/design notes on this exact distinction.
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    plugin_dir = vault_path / ".obsidian" / "plugins" / "some-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "main.js").write_text("console.log(1)")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    rows = all_changes(repo)
    assert [r["path"] for r in rows] == [".obsidian/plugins/some-plugin/main.js"]


def test_delete_on_server_records_a_tombstone_change(tmp_path):
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    (vault_path / "a.md").write_text("hello")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")

    servicer._delete_on_server(str(vault_path), "alice", "vault-1", "a.md", {"content_hash": "h", "size_bytes": 5, "modified_at_ms": 1}, 5000, 4000, "dev", "user")

    rows = all_changes(repo)
    assert rows[-1]["path"] == "a.md"
    assert rows[-1]["is_deleted"] is True


def test_finalize_pushed_file_records_a_change(tmp_path):
    # PR6 of #14_옵시디언싱크_정렬_구현계획.md replaced the batch-oriented
    # UploadFiles/_finalize_uploaded_file (gone -- see llm-wiki) with this per-file push_req
    # finalize path; same mutation-site-journals-a-change coverage, updated call site.
    import hashlib
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    content = b"hello push"

    ok, err = servicer.finalize_pushed_file(
        "alice", "vault-1", "a.md", content, len(content), 1000,
        hashlib.sha256(content).hexdigest(), "dev", "user",
    )

    assert ok is True, err
    rows = all_changes(repo)
    assert [r["path"] for r in rows] == ["a.md"]
    assert rows[0]["content_hash"] == hashlib.sha256(content).hexdigest()


def test_restore_history_version_records_a_change(tmp_path):
    import asyncio
    import hashlib
    from pumice_server import wire_types
    from pumice_server.service import SyncServiceServicer

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    vault_path = tmp_path / "vaults" / "alice" / "vault-1"
    vault_path.mkdir(parents=True)
    note = vault_path / "a.md"
    note.write_text("v1")

    class VerifyingServicer(SyncServiceServicer):
        def _verify_vault_access(self, vault_id, context):
            return "alice"

    servicer = VerifyingServicer(data_dir=str(tmp_path), repository=repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault-1")
    history_id = repo.get_history("alice", "vault-1", "a.md")[0]["history_id"]

    class FakeContext:
        def invocation_metadata(self):
            return [("authorization", "Bearer tok"), ("x-device-name", "dev"), ("x-user-name", "user")]

    request = wire_types.RestoreHistoryRequest(vault_id="vault-1", path="a.md", history_id=history_id)

    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert response.ok, response.error
    rows = all_changes(repo)
    assert rows[-1]["path"] == "a.md"
    assert rows[-1]["is_deleted"] is False


# ─── PurgeVault leaves tombstones behind ─────────────────────────────────────────────────────


def test_purge_vault_records_a_tombstone_for_every_currently_live_path(repo):
    meta_a = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "ha", "is_deleted": False}
    meta_b = {"path": "b.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "hb", "is_deleted": False}
    repo.save_one("alice", "vault-1", "a.md", meta_a)
    repo.save_one("alice", "vault-1", "b.md", meta_b)
    repo.record_change("alice", "vault-1", "a.md", meta_a)
    repo.record_change("alice", "vault-1", "b.md", meta_b)

    repo.purge_vault("alice", "vault-1")

    rows = all_changes(repo)
    tombstones = [r for r in rows if r["is_deleted"]]
    assert {r["path"] for r in tombstones} == {"a.md", "b.md"}


def test_purge_vault_does_not_record_a_tombstone_for_an_already_deleted_path(repo):
    meta_a = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "ha", "is_deleted": False}
    meta_deleted = {"path": "b.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True}
    repo.save_one("alice", "vault-1", "a.md", meta_a)
    repo.save_one("alice", "vault-1", "b.md", meta_deleted)

    repo.purge_vault("alice", "vault-1")

    rows = all_changes(repo)
    tombstones = [r for r in rows if r["is_deleted"]]
    assert {r["path"] for r in tombstones} == {"a.md"}  # not b.md -- already a tombstone


# ─── get_changes_since / get_latest_change_id (PR2 read side) ──────────────────────────────


def test_get_latest_change_id_is_zero_for_a_vault_with_no_changes(repo):
    assert repo.get_latest_change_id("alice", "vault-1") == 0


def test_get_latest_change_id_returns_the_highest_change_id_for_this_owner_and_vault(repo):
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}
    repo.record_change("alice", "vault-1", "a.md", meta)
    repo.record_change("alice", "vault-1", "b.md", meta)
    repo.record_change("bob", "vault-1", "z.md", meta)  # different owner, must not affect the count

    latest = repo.get_latest_change_id("alice", "vault-1")

    rows = all_changes(repo)
    assert latest == rows[-1]["change_id"]


def test_get_changes_since_returns_nothing_at_or_before_the_given_baseline(repo):
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}
    repo.record_change("alice", "vault-1", "a.md", meta)
    latest = repo.get_latest_change_id("alice", "vault-1")

    assert repo.get_changes_since("alice", "vault-1", latest) == []


def test_get_changes_since_returns_changes_strictly_after_the_baseline(repo):
    meta_a = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "ha", "is_deleted": False}
    meta_b = {"path": "b.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "hb", "is_deleted": False}
    repo.record_change("alice", "vault-1", "a.md", meta_a)
    baseline = repo.get_latest_change_id("alice", "vault-1")
    repo.record_change("alice", "vault-1", "b.md", meta_b)

    changes = repo.get_changes_since("alice", "vault-1", baseline)

    assert [c["path"] for c in changes] == ["b.md"]
    assert changes[0]["content_hash"] == "hb"


def test_get_changes_since_folds_multiple_changes_to_the_same_path_into_the_latest_only(repo):
    # A client resuming from far behind must see the final state, not a replay of every
    # intermediate version -- same semantic Delta already has for "current state".
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "h2", "is_deleted": False})
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 3, "size_bytes": 3, "content_hash": "h3", "is_deleted": False})

    changes = repo.get_changes_since("alice", "vault-1", 0)

    assert len(changes) == 1
    assert changes[0]["content_hash"] == "h3"


def test_get_changes_since_includes_tombstones_even_across_a_delete_then_recreate_sequence(repo):
    # Mirrors test_service_delta.py's tombstone-propagation invariant for Delta -- a client
    # resuming from before a delete-then-recreate pair must land on the final (recreated) state,
    # not silently skip the fact that it was ever deleted in between (folding already handles
    # that here since only the LAST row per path survives, but this pins the exact scenario).
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True})
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 3, "size_bytes": 5, "content_hash": "h3", "is_deleted": False})

    changes = repo.get_changes_since("alice", "vault-1", 0)

    assert len(changes) == 1
    assert changes[0]["is_deleted"] is False
    assert changes[0]["content_hash"] == "h3"


def test_get_changes_since_a_pure_tombstone_reaches_the_client(repo):
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    baseline = repo.get_latest_change_id("alice", "vault-1")
    repo.record_change("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True})

    changes = repo.get_changes_since("alice", "vault-1", baseline)

    assert changes == [{"path": "a.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True}]


def test_get_changes_since_is_scoped_by_owner_and_vault(repo):
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}
    repo.record_change("alice", "vault-1", "a.md", meta)
    repo.record_change("bob", "vault-1", "a.md", meta)
    repo.record_change("alice", "vault-2", "a.md", meta)

    assert len(repo.get_changes_since("alice", "vault-1", 0)) == 1
    assert len(repo.get_changes_since("bob", "vault-1", 0)) == 1
    assert len(repo.get_changes_since("alice", "vault-2", 0)) == 1


def test_purge_vault_still_returns_history_backup_paths_unchanged(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    repo.save_one("alice", "vault-1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False})
    repo.add_history("alice", "vault-1", "a.md", 1, 1, "h", "/tmp/some.bak", "dev", "user")

    backup_paths = repo.purge_vault("alice", "vault-1")

    assert backup_paths == ["/tmp/some.bak"]
