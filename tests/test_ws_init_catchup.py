"""TDD for `init`'s version catch-up burst -- PR2 of #14_옵시디언싱크_정렬_구현계획.md (pumice
repo). Confirmed via direct obsidian.asar analysis that real Obsidian Sync has no separate
"give me changes since X" RPC: the client reports its last known version in `init`, and the
server just streams the intervening changes as ordinary `push` frames (the exact op the client
already handles for live updates), then signals `ready`. This intentionally reuses
tests/test_ws_sync_rpc_ops.py's pattern (real SqlAlchemyMetadataRepository + real
SyncServiceServicer, not fakes) since the whole point is exercising the real
repository.get_changes_since()/get_latest_change_id() round trip through SyncWsSession, not
re-verifying business logic other tests already cover.
"""

import json

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer
from pumice_server.ws_sync_resource import SyncWsSession


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class RecordingSender:
    def __init__(self):
        self.sent = []

    def __call__(self, message: dict) -> None:
        self.sent.append(message)

    def find(self, op):
        return [m for m in self.sent if m["op"] == op]


def make_session(tmp_path, repo, servicer=None):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = servicer or SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender)
    return session, sender


def send_init(session, last_known_change_id=None):
    payload = {"token": "tok", "vaultId": "vault1"}
    if last_known_change_id is not None:
        payload["lastKnownChangeId"] = last_known_change_id
    session.handle_text_message(json.dumps({"op": "init", "payload": payload}))


def test_init_without_a_baseline_sends_no_catchup_push_but_reports_the_current_latest(tmp_path, repo):
    # A file already changed server-side before this device ever connected.
    meta = {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False}
    repo.record_change("alice", "vault1", "a.md", meta)
    latest = repo.get_latest_change_id("alice", "vault1")

    session, sender = make_session(tmp_path, repo)
    send_init(session)  # lastKnownChangeId omitted -- brand-new device

    assert sender.find("push") == []
    ready = sender.find("ready")[-1]
    assert ready["payload"]["latestChangeId"] == latest


def test_init_with_a_stale_baseline_streams_intervening_changes_as_push_frames(tmp_path, repo):
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    baseline = repo.get_latest_change_id("alice", "vault1")
    repo.record_change("alice", "vault1", "b.md", {"path": "b.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "h2", "is_deleted": False})
    latest = repo.get_latest_change_id("alice", "vault1")

    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=baseline)

    pushes = sender.find("push")
    assert len(pushes) == 1
    assert pushes[0]["payload"] == {
        "vaultId": "vault1", "path": "b.md", "modifiedAtMs": 2, "sizeBytes": 2, "contentHash": "h2", "isDeleted": False,
    }
    ready = sender.find("ready")[-1]
    assert ready["payload"]["latestChangeId"] == latest
    # push frames must land before ready, so the client can rely on "ready means caught up".
    ops_in_order = [m["op"] for m in sender.sent]
    assert ops_in_order.index("push") < ops_in_order.index("ready")


def test_init_with_an_up_to_date_baseline_sends_no_catchup_push(tmp_path, repo):
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    latest = repo.get_latest_change_id("alice", "vault1")

    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=latest)

    assert sender.find("push") == []
    assert sender.find("ready")[-1]["payload"]["latestChangeId"] == latest


def test_init_catchup_folds_multiple_changes_to_the_same_path_into_one_push(tmp_path, repo):
    # last_known_change_id=0 means "skip catch-up" (brand-new device, see the design doc), so
    # the baseline captured here must be non-zero -- seed an unrelated change first.
    repo.record_change("alice", "vault1", "seed.md", {"path": "seed.md", "modified_at_ms": 0, "size_bytes": 0, "content_hash": "s", "is_deleted": False})
    baseline = repo.get_latest_change_id("alice", "vault1")
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 2, "content_hash": "h2", "is_deleted": False})

    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=baseline)

    pushes = sender.find("push")
    assert len(pushes) == 1
    assert pushes[0]["payload"]["contentHash"] == "h2"


def test_init_catchup_delivers_a_tombstone_for_a_delete_then_recreate_sequence(tmp_path, repo):
    # Mirrors test_service_delta.py's tombstone-propagation invariant for Delta -- a device
    # resuming from before a delete-then-recreate pair must land on the final (recreated) state
    # via a single push, not silently miss the fact that it was ever deleted in between.
    # last_known_change_id=0 means "skip catch-up" (brand-new device), so seed an unrelated
    # change first to get a non-zero baseline to resume from.
    repo.record_change("alice", "vault1", "seed.md", {"path": "seed.md", "modified_at_ms": 0, "size_bytes": 0, "content_hash": "s", "is_deleted": False})
    baseline = repo.get_latest_change_id("alice", "vault1")
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True})
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 3, "size_bytes": 5, "content_hash": "h3", "is_deleted": False})

    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=baseline)

    pushes = sender.find("push")
    assert len(pushes) == 1
    assert pushes[0]["payload"]["isDeleted"] is False
    assert pushes[0]["payload"]["contentHash"] == "h3"


def test_init_catchup_delivers_a_pure_tombstone(tmp_path, repo):
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h1", "is_deleted": False})
    baseline = repo.get_latest_change_id("alice", "vault1")
    repo.record_change("alice", "vault1", "a.md", {"path": "a.md", "modified_at_ms": 2, "size_bytes": 0, "content_hash": "", "is_deleted": True})

    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=baseline)

    [push] = sender.find("push")
    assert push["payload"]["isDeleted"] is True
    assert push["payload"]["path"] == "a.md"


def test_init_catchup_is_scoped_to_the_authenticated_owner_and_requested_vault(tmp_path, repo):
    repo.create_device_token("tok-bob", "bob", "bobs-device", 0)
    repo.record_change("bob", "vault1", "secret.md", {"path": "secret.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False})
    repo.record_change("alice", "vault2", "other-vault.md", {"path": "other-vault.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False})

    # A stale-but-positive baseline for (alice, vault1) -- nothing was ever recorded there, so
    # bob's and alice's-other-vault's changes above must never leak into this catch-up.
    session, sender = make_session(tmp_path, repo)
    send_init(session, last_known_change_id=1)

    assert sender.find("push") == []
