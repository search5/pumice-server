"""TDD for the WS delta/restore/history-list ops and the push (ChangeNotifier) wiring --
PR2 of #11_websocket_동기화_프로토콜_설계.md.

SyncServiceServicer's Delta/GetFileHistory/RestoreHistoryVersion are called through their
normal (wire_types-speaking, see pumice's 13_PR5_계획.md) interface -- these tests exercise
the WS-specific glue: JSON payload <-> wire_types conversion, WsServicerContext
(invocation_metadata()/abort() synthesized from the session's already-authenticated identity
instead of a header), and SyncWsSession's dispatch of async ops (as opposed to the synchronous
init/ping path covered by test_ws_sync_session.py).

Uses the real SqlAlchemyMetadataRepository + SyncServiceServicer (same pattern as
test_service_delta.py) rather than a fake -- the whole point of this PR is the WS<->servicer
round trip actually working, not re-verifying business logic those tests already cover.
"""

import asyncio

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer
from pumice_server.change_notifier import ChangeNotifier
from pumice_server.status_codes import StatusCode
from pumice_server.ws_sync_resource import SyncWebSocketProtocol, SyncWsSession, WsServicerContext, WsAborted


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

    @property
    def last(self):
        return self.sent[-1]

    def find(self, op):
        return [m for m in self.sent if m["op"] == op]


def make_authenticated_session(tmp_path, repo, servicer=None, on_authenticated=None):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = servicer or SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender, on_authenticated=on_authenticated)
    session.handle_text_message('{"op":"init","payload":{"token":"tok","vaultId":"vault1","deviceName":"MyPC","userName":"Alice"}}')
    assert session.authenticated
    return session, sender, servicer


def run(coro):
    if coro is not None:
        asyncio.run(coro)


# ─── WsServicerContext ──────────────────────────────────────────────────────────────────────


def test_ws_servicer_context_synthesizes_invocation_metadata_from_session():
    class FakeSession:
        token = "tok"
        device_name = "my device"
        user_name = "a user"
        owner_username = "alice"

    context = WsServicerContext(FakeSession())

    metadata = dict(context.invocation_metadata())
    assert metadata["authorization"] == "Bearer tok"
    # url-encoded, matching what service.py's unquote() call sites expect
    assert metadata["x-device-name"] == "my%20device"
    assert metadata["x-user-name"] == "a%20user"
    assert metadata["x-vault-owner"] == "alice"


def test_ws_servicer_context_synthesizes_empty_vault_owner_when_session_has_none():
    class FakeSession:
        token = "tok"
        device_name = ""
        user_name = ""
        owner_username = None

    context = WsServicerContext(FakeSession())

    assert dict(context.invocation_metadata())["x-vault-owner"] == ""


def test_ws_servicer_context_abort_raises_ws_aborted_with_code_and_details():
    context = WsServicerContext(session=object())

    with pytest.raises(WsAborted) as exc_info:
        context.abort(StatusCode.UNAUTHENTICATED, "nope")

    assert exc_info.value.code == StatusCode.UNAUTHENTICATED
    assert exc_info.value.details == "nope"


# ─── delta_req / delta_res ──────────────────────────────────────────────────────────────────


def test_delta_req_returns_need_download_for_a_server_side_file(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("hello")

    session, sender, _ = make_authenticated_session(tmp_path, repo)

    coro = session.handle_text_message('{"op":"delta_req","payload":{"vaultId":"vault1","localFiles":[]}}')
    run(coro)

    res = sender.last
    assert res["op"] == "delta_res"
    assert res["payload"]["needUpload"] == []
    [meta] = res["payload"]["needDownload"]
    assert meta["path"] == "note.md"
    assert meta["isDeleted"] is False
    assert "contentHash" in meta and "modifiedAtMs" in meta and "sizeBytes" in meta


def test_delta_req_returns_need_upload_for_a_client_only_file(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)

    session, sender, _ = make_authenticated_session(tmp_path, repo)

    payload = {
        "vaultId": "vault1",
        "localFiles": [{"path": "local.md", "modifiedAtMs": 1000, "sizeBytes": 3, "contentHash": "abc", "isDeleted": False}],
    }
    run(session.handle_text_message('{"op":"delta_req","payload":' + __import__("json").dumps(payload) + "}"))

    res = sender.last
    assert res["payload"]["needUpload"] == ["local.md"]
    assert res["payload"]["needDownload"] == []


# ─── history_req / history_res + restore_req / restore_res ────────────────────────────────


def test_history_then_restore_round_trip(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    note = vault_path / "note.md"
    note.write_text("v1")

    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    # First scan creates a history entry for note.md (see _scan_and_merge's backup-on-first-seen).
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    run(session.handle_text_message('{"op":"history_req","payload":{"vaultId":"vault1","path":"note.md"}}'))
    history_res = sender.find("history_res")[-1]
    [version] = history_res["payload"]["versions"]
    assert version["contentHash"]
    assert version["deleted"] is False
    assert version["relatedPath"] is None
    history_id = version["historyId"]

    # _backup_file hard-links rather than copies (see service.py) -- an in-place write here
    # (Path.write_text truncates the existing inode) would silently corrupt that shared backup
    # too, the same way a raw write would in production. Real vault writes always go through a
    # temp-file + os.rename() instead, which detaches the directory entry onto a fresh inode
    # and leaves the hard-linked backup's content alone -- mimic that here.
    tmp_note = note.with_suffix(".md.tmp")
    tmp_note.write_text("v2 -- overwritten locally")
    tmp_note.replace(note)
    payload = {"vaultId": "vault1", "path": "note.md", "historyId": history_id}
    run(session.handle_text_message('{"op":"restore_req","payload":' + __import__("json").dumps(payload) + "}"))

    restore_res = sender.find("restore_res")[-1]
    assert restore_res["payload"] == {"ok": True, "error": ""}
    assert note.read_text() == "v1"


def test_history_req_for_unknown_path_returns_empty_versions(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    session, sender, _ = make_authenticated_session(tmp_path, repo)

    run(session.handle_text_message('{"op":"history_req","payload":{"vaultId":"vault1","path":"nope.md"}}'))

    assert sender.last["payload"]["versions"] == []


# ─── size_req / purge_req / usernames_req (2026-08 Obsidian core fidelity follow-up) ───────


def test_size_req_returns_vault_and_total_size(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("hello")
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    run(session.handle_text_message('{"op":"size_req","payload":{"vaultId":"vault1"}}'))

    assert sender.last["op"] == "size_res"
    assert sender.last["payload"] == {"vaultSizeBytes": 5, "totalSizeBytes": 5, "limitBytes": -1}


def test_purge_req_removes_the_vault_and_a_later_delta_sees_nothing_left(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("hello")
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    run(session.handle_text_message('{"op":"purge_req","payload":{"vaultId":"vault1"}}'))

    assert sender.last["op"] == "purge_res"
    assert sender.last["payload"] == {"ok": True, "error": ""}
    assert not vault_path.exists()

    run(session.handle_text_message('{"op":"delta_req","payload":{"vaultId":"vault1","localFiles":[]}}'))
    assert sender.last["payload"]["needDownload"] == []


def test_usernames_req_returns_only_the_caller(tmp_path, repo):
    session, sender, _ = make_authenticated_session(tmp_path, repo)

    run(session.handle_text_message('{"op":"usernames_req","payload":{"vaultId":"vault1"}}'))

    assert sender.last["op"] == "usernames_res"
    assert sender.last["payload"] == {"usernames": ["alice"]}


# ─── vault sharing: a collaborator's WS session end to end (14_vault_sharing_설계.md) ──────────


def make_collaborator_session(tmp_path, repo, servicer=None):
    """A real (not faked) owner+collaborator pair: alice owns vault1, bob has an accepted
    share for it, and this returns bob's own authenticated session -- every op it issues below
    goes through the real resolve_vault_owner()/x-vault-owner plumbing end to end."""
    repo.create_device_token("alice-tok", "alice", "alice-device", 0)
    repo.create_device_token("bob-tok", "bob", "bob-device", 0)
    repo.create_user("bob", "hash", email="bob@example.com")
    repo.add_vault_share("alice", "vault1", "bob@example.com")
    servicer = servicer or SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender)
    session.handle_text_message(
        '{"op":"init","payload":{"token":"bob-tok","vaultId":"vault1","vaultOwner":"alice","deviceName":"BobPC","userName":"Bob"}}'
    )
    assert session.authenticated
    assert session.owner_username == "alice"
    return session, sender, servicer


def test_collaborator_delta_req_sees_the_owners_vault_data(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("hello")

    session, sender, servicer = make_collaborator_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    run(session.handle_text_message('{"op":"delta_req","payload":{"vaultId":"vault1","localFiles":[]}}'))

    [meta] = sender.last["payload"]["needDownload"]
    assert meta["path"] == "note.md"


def test_collaborator_usernames_req_lists_owner_and_self(tmp_path, repo):
    session, sender, _ = make_collaborator_session(tmp_path, repo)

    run(session.handle_text_message('{"op":"usernames_req","payload":{"vaultId":"vault1"}}'))

    assert sender.last["payload"] == {"usernames": ["alice", "bob"]}


def test_a_share_for_one_vault_does_not_grant_access_to_a_different_vault(tmp_path, repo):
    repo.create_device_token("alice-tok", "alice", "alice-device", 0)
    repo.create_device_token("bob-tok", "bob", "bob-device", 0)
    repo.create_user("bob", "hash", email="bob@example.com")
    repo.add_vault_share("alice", "vaultA", "bob@example.com")
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender)

    session.handle_text_message(
        '{"op":"init","payload":{"token":"bob-tok","vaultId":"vaultB","vaultOwner":"alice"}}'
    )

    assert session.authenticated is False
    assert sender.last["payload"]["code"] == "PERMISSION_DENIED"


# ─── push (ChangeNotifier wiring via on_authenticated) ─────────────────────────────────────


def test_session_calls_on_authenticated_after_successful_init(tmp_path, repo):
    calls = []
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender, on_authenticated=lambda owner, vault: calls.append((owner, vault)))

    session.handle_text_message('{"op":"init","payload":{"token":"tok","vaultId":"vault1"}}')

    assert calls == [("alice", "vault1")]


def test_on_authenticated_is_not_called_when_init_fails(tmp_path, repo):
    calls = []
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender, on_authenticated=lambda owner, vault: calls.append((owner, vault)))

    session.handle_text_message('{"op":"init","payload":{"token":"wrong","vaultId":"vault1"}}')

    assert calls == []


def test_change_notifier_push_reaches_a_subscribed_ws_session(tmp_path, repo):
    notifier = ChangeNotifier()
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=notifier)
    sender = RecordingSender()

    pushes = []

    def on_authenticated(owner, vault_id):
        notifier.subscribe(
            owner,
            vault_id,
            lambda file_meta: sender({
                "op": "push",
                "payload": {
                    "vaultId": vault_id,
                    "path": file_meta["path"],
                    "modifiedAtMs": file_meta["modified_at_ms"],
                    "sizeBytes": file_meta["size_bytes"],
                    "contentHash": file_meta["content_hash"],
                    "isDeleted": file_meta["is_deleted"],
                },
            }),
        )

    session = SyncWsSession(repo, servicer, sender, on_authenticated=on_authenticated)
    session.handle_text_message('{"op":"init","payload":{"token":"tok","vaultId":"vault1"}}')

    notifier.notify_change("alice", "vault1", {"path": "a.md", "modified_at_ms": 1000, "size_bytes": 5, "content_hash": "h1", "is_deleted": False})

    push = sender.find("push")[-1]
    assert push == {
        "op": "push",
        "payload": {"vaultId": "vault1", "path": "a.md", "modifiedAtMs": 1000, "sizeBytes": 5, "contentHash": "h1", "isDeleted": False},
    }


def test_ws_protocol_on_change_sends_push_with_real_file_metadata(tmp_path, repo):
    """Exercises SyncWebSocketProtocol's actual _on_authenticated/_on_change wiring (not a
    hand-rolled stand-in like the test above) -- the production code that runs a real WS
    connection through, see ws_sync_resource.py."""
    notifier = ChangeNotifier()
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=notifier)
    protocol = SyncWebSocketProtocol(repo, servicer, notifier)
    sent = []
    protocol._send_json = sent.append

    class FakeSession:
        vault_id = "vault1"

    protocol._session = FakeSession()
    protocol._on_authenticated("alice", "vault1")

    file_meta = {"path": "a.md", "modified_at_ms": 1000, "size_bytes": 5, "content_hash": "h1", "is_deleted": False}
    notifier.notify_change("alice", "vault1", file_meta)

    assert sent == [{
        "op": "push",
        "payload": {"vaultId": "vault1", "path": "a.md", "modifiedAtMs": 1000, "sizeBytes": 5, "contentHash": "h1", "isDeleted": False},
    }]
