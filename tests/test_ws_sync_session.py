"""TDD for SyncWsSession: the per-connection WebSocket sync protocol logic, deliberately kept
independent of autobahn/Twisted transport specifics (mirrors NativeServicerContext vs the
business logic it wraps in grpc_web_resource.py/service.py) so it's testable without a real
socket. `send` is an injected callable instead of a transport method call.

See #11_websocket_동기화_프로토콜_설계.md / #14_옵시디언싱크_정렬_구현계획.md (pumice repo) for
the wire protocol this implements: JSON text frames, {op, payload} envelope (no requestId --
exactly one request may be outstanding at a time, matching Obsidian core's own Sync client),
init-first handshake (no custom headers -- browser WebSocket API doesn't support them),
ping/pong heartbeat response.
"""

import json

import pytest

from pumice_server.repository import VaultAccessDenied
from pumice_server.ws_sync_resource import SyncWsSession


class FakeRepository:
    """`devices` and `shares` are plain public dicts a test can poke directly (e.g. add a
    second account's token, or grant/deny a vault share) instead of needing constructor
    params for every scenario. `shares` keys are (owner_username, vault_id, caller_username)
    -> accepted bool, mirroring resolve_vault_owner()'s real contract (see
    tests/test_vault_share_resolver.py for the real-repository version of this logic)."""

    def __init__(self, token="tok", username="alice"):
        self.devices = {token: username}
        self.shares = {}

    def get_device_token(self, token):
        username = self.devices.get(token)
        return {"username": username} if username else None

    def get_changes_since(self, owner_username, vault_id, since_change_id):
        return []

    def get_latest_change_id(self, owner_username, vault_id):
        return 0

    def resolve_vault_owner(self, caller_username, requested_owner_username, vault_id, mark_accepted=False):
        if not requested_owner_username or requested_owner_username == caller_username:
            return caller_username
        key = (requested_owner_username, vault_id, caller_username)
        if key not in self.shares:
            raise VaultAccessDenied(f"no access for {caller_username} to {requested_owner_username}/{vault_id}")
        if mark_accepted:
            self.shares[key] = True
        return requested_owner_username


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


class FakeServicer:
    """Stands in for the real SyncServiceServicer -- none of these tests exercise an actual RPC
    op (that needs the real thing, see test_ws_sync_rpc_ops.py), only init/ping, but init_ok now
    reads max_file_size_bytes off the servicer to advertise as maxFileSizeBytes (see
    #11_websocket_동기화_프로토콜_설계.md's perFileMax re-analysis)."""

    def __init__(self, max_file_size_bytes=2 * 1024 ** 3):
        self.max_file_size_bytes = max_file_size_bytes


def make_session(repository=None, servicer=None):
    repository = repository or FakeRepository()
    servicer = servicer if servicer is not None else FakeServicer()
    sender = RecordingSender()
    session = SyncWsSession(repository, servicer, sender)
    return session, sender


def send_envelope(session, op, payload=None):
    session.handle_text_message(json.dumps({"op": op, "payload": payload or {}}))


def test_init_with_valid_token_authenticates_and_sends_init_ok():
    session, sender = make_session()

    send_envelope(session, "init", {
        "token": "tok", "vaultId": "vault1", "deviceName": "MyPC", "userName": "Alice",
    })

    assert session.authenticated is True
    assert session.owner_username == "alice"
    assert session.vault_id == "vault1"
    assert session.device_name == "MyPC"
    assert session.user_name == "Alice"
    init_ok = sender.find("init_ok")[-1]
    assert "serverVersion" in init_ok["payload"]
    # A fresh session always gets a `ready` after `init_ok` too (PR2's version catch-up --
    # see #14_옵시디언싱크_정렬_구현계획.md) -- FakeRepository has no changes, so it just reports
    # latestChangeId 0, no catch-up push frames.
    ready = sender.find("ready")[-1]
    assert ready["payload"]["latestChangeId"] == 0
    assert sender.find("push") == []


def test_init_ok_advertises_the_servicer_s_configured_max_file_size():
    session, sender = make_session(servicer=FakeServicer(max_file_size_bytes=12345))

    send_envelope(session, "init", {"token": "tok", "vaultId": "vault1"})

    assert sender.find("init_ok")[-1]["payload"]["maxFileSizeBytes"] == 12345


def test_init_with_invalid_token_sends_unauthenticated_error():
    session, sender = make_session()

    send_envelope(session, "init", {"token": "wrong", "vaultId": "vault1"})

    assert session.authenticated is False
    assert sender.last == {
        "op": "error",
        "payload": {"code": "UNAUTHENTICATED", "message": "Invalid or missing auth token"},
    }


@pytest.mark.parametrize("payload", [
    {"vaultId": "vault1"},  # missing token
    {"token": "tok"},       # missing vaultId
    {},
])
def test_init_missing_required_fields_sends_invalid_argument_error(payload):
    session, sender = make_session()

    send_envelope(session, "init", payload)

    assert session.authenticated is False
    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INVALID_ARGUMENT"


def test_any_op_other_than_init_before_authentication_is_rejected():
    session, sender = make_session()

    send_envelope(session, "ping")

    assert session.authenticated is False
    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "UNAUTHENTICATED"


def test_ping_after_init_returns_pong():
    session, sender = make_session()
    send_envelope(session, "init", {"token": "tok", "vaultId": "vault1"})

    send_envelope(session, "ping")

    assert sender.last["op"] == "pong"
    assert "timestampMs" in sender.last["payload"]


def test_unknown_op_after_init_returns_error_without_raising():
    session, sender = make_session()
    send_envelope(session, "init", {"token": "tok", "vaultId": "vault1"})

    send_envelope(session, "not_a_real_op")

    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "UNKNOWN_OP"


def test_malformed_json_does_not_raise_and_sends_error():
    session, sender = make_session()

    session.handle_text_message("{not json")

    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INVALID_JSON"


# ─── vault sharing: init with vaultOwner (see 14_vault_sharing_설계.md, pumice repo) ────────────

def test_init_without_vault_owner_is_unaffected_self_owned_as_before():
    session, sender = make_session()

    send_envelope(session, "init", {"token": "tok", "vaultId": "vault1"})

    assert session.authenticated is True
    assert session.owner_username == "alice"
    assert sender.find("error") == []


def test_init_with_vault_owner_equal_to_caller_is_the_same_as_omitting_it():
    session, sender = make_session()

    send_envelope(session, "init", {"token": "tok", "vaultId": "vault1", "vaultOwner": "alice"})

    assert session.authenticated is True
    assert session.owner_username == "alice"


def test_init_with_a_granted_share_connects_as_the_requested_owner():
    repo = FakeRepository(token="tok", username="alice")
    repo.devices["bob-tok"] = "bob"
    repo.shares[("alice", "vault1", "bob")] = False  # pending, not yet accepted
    session, sender = make_session(repository=repo)

    send_envelope(session, "init", {"token": "bob-tok", "vaultId": "vault1", "vaultOwner": "alice"})

    assert session.authenticated is True
    assert session.owner_username == "alice"
    assert sender.find("error") == []
    # A pending share is enough for access (binary, no role gate) -- but the live connection
    # itself is what flips it to accepted (mark_accepted=True from _handle_init).
    assert repo.shares[("alice", "vault1", "bob")] is True


def test_init_with_no_share_at_all_is_rejected_with_permission_denied():
    repo = FakeRepository(token="tok", username="alice")
    repo.devices["bob-tok"] = "bob"
    session, sender = make_session(repository=repo)

    send_envelope(session, "init", {"token": "bob-tok", "vaultId": "vault1", "vaultOwner": "alice"})

    assert session.authenticated is False
    assert session.owner_username is None
    assert sender.last == {
        "op": "error",
        "payload": {"code": "PERMISSION_DENIED", "message": "No access to this vault"},
    }


def test_init_with_a_share_for_a_different_vault_id_is_still_rejected():
    repo = FakeRepository(token="tok", username="alice")
    repo.devices["bob-tok"] = "bob"
    repo.shares[("alice", "vaultA", "bob")] = True
    session, sender = make_session(repository=repo)

    send_envelope(session, "init", {"token": "bob-tok", "vaultId": "vaultB", "vaultOwner": "alice"})

    assert session.authenticated is False
    assert sender.last["payload"]["code"] == "PERMISSION_DENIED"
