"""TDD for SyncWsSession: the per-connection WebSocket sync protocol logic, deliberately kept
independent of autobahn/Twisted transport specifics (mirrors NativeServicerContext vs the
business logic it wraps in grpc_web_resource.py/service.py) so it's testable without a real
socket. `send` is an injected callable instead of a transport method call.

See #11_websocket_동기화_프로토콜_설계.md for the wire protocol this implements: JSON text
frames, {requestId, op, payload} envelope, init-first handshake (no custom headers -- browser
WebSocket API doesn't support them), ping/pong heartbeat response.
"""

import json

import pytest

from pumice_server.ws_sync_resource import SyncWsSession


class FakeRepository:
    def __init__(self, token="tok", username="alice"):
        self._token = token
        self._username = username

    def get_device_token(self, token):
        if token == self._token:
            return {"username": self._username}
        return None


class RecordingSender:
    def __init__(self):
        self.sent = []

    def __call__(self, message: dict) -> None:
        self.sent.append(message)

    @property
    def last(self):
        return self.sent[-1]


def make_session(repository=None):
    repository = repository or FakeRepository()
    sender = RecordingSender()
    session = SyncWsSession(repository, sender)
    return session, sender


def send_envelope(session, request_id, op, payload=None):
    session.handle_text_message(json.dumps({"requestId": request_id, "op": op, "payload": payload or {}}))


def test_init_with_valid_token_authenticates_and_sends_init_ok():
    session, sender = make_session()

    send_envelope(session, 1, "init", {
        "token": "tok", "vaultId": "vault1", "deviceName": "MyPC", "userName": "Alice",
    })

    assert session.authenticated is True
    assert session.owner_username == "alice"
    assert session.vault_id == "vault1"
    assert session.device_name == "MyPC"
    assert session.user_name == "Alice"
    assert sender.last["requestId"] == 1
    assert sender.last["op"] == "init_ok"
    assert "maxInFlight" in sender.last["payload"]


def test_init_with_invalid_token_sends_unauthenticated_error():
    session, sender = make_session()

    send_envelope(session, 1, "init", {"token": "wrong", "vaultId": "vault1"})

    assert session.authenticated is False
    assert sender.last == {
        "requestId": 1, "op": "error",
        "payload": {"code": "UNAUTHENTICATED", "message": "Invalid or missing auth token"},
    }


@pytest.mark.parametrize("payload", [
    {"vaultId": "vault1"},  # missing token
    {"token": "tok"},       # missing vaultId
    {},
])
def test_init_missing_required_fields_sends_invalid_argument_error(payload):
    session, sender = make_session()

    send_envelope(session, 1, "init", payload)

    assert session.authenticated is False
    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INVALID_ARGUMENT"


def test_any_op_other_than_init_before_authentication_is_rejected():
    session, sender = make_session()

    send_envelope(session, 5, "ping")

    assert session.authenticated is False
    assert sender.last["requestId"] == 5
    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "UNAUTHENTICATED"


def test_ping_after_init_returns_pong_with_same_request_id():
    session, sender = make_session()
    send_envelope(session, 1, "init", {"token": "tok", "vaultId": "vault1"})

    send_envelope(session, 42, "ping")

    assert sender.last["requestId"] == 42
    assert sender.last["op"] == "pong"
    assert "timestampMs" in sender.last["payload"]


def test_unknown_op_after_init_returns_error_without_raising():
    session, sender = make_session()
    send_envelope(session, 1, "init", {"token": "tok", "vaultId": "vault1"})

    send_envelope(session, 2, "not_a_real_op")

    assert sender.last["requestId"] == 2
    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "UNKNOWN_OP"


def test_malformed_json_does_not_raise_and_sends_error():
    session, sender = make_session()

    session.handle_text_message("{not json")

    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INVALID_JSON"
