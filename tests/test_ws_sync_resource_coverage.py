"""Coverage follow-up for ws_sync_resource.py's error/edge paths not exercised by
test_ws_sync_session.py (init/ping happy paths), test_ws_sync_rpc_ops.py (unary op happy
paths + WsServicerContext basics) or test_ws_sync_streaming_ops.py (streaming op happy
paths): servicer failures surfacing through _run_request/_run_stream_request/_finish_upload
as an `error` op instead of crashing the session, the upload accumulator's INVALID_STATE
rejections, the STREAM_IN_PROGRESS rejection specifically via a second upload_begin (as
opposed to the other streaming-op-vs-streaming-op case already covered elsewhere), a
FileChunk with no oneof member set, and SyncWebSocketProtocol's autobahn-glue methods
(onOpen/onMessage/onClose/_on_init_timeout/_send_json/_send_binary) plus
make_ws_sync_resource -- none of which any existing test file drives directly.
"""

import asyncio
import hashlib
import json
import logging

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer
from pumice_server.status_codes import StatusCode
from pumice_server.ws_sync_resource import (
    INIT_TIMEOUT_SECONDS,
    SyncWebSocketProtocol,
    SyncWsSession,
    _file_chunk_to_frame,
    make_ws_sync_resource,
)
from pumice_server import wire_types
from autobahn.twisted.resource import WebSocketResource


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakeRepository:
    """Enough of MetadataRepository for _handle_init's own token lookup -- these tests care
    about what happens *after* auth, so a single fixed valid token is all that's needed."""

    def get_device_token(self, token):
        if token == "tok":
            return {"username": "alice"}
        return None

    def get_changes_since(self, owner_username, vault_id, since_change_id):
        return []

    def get_latest_change_id(self, owner_username, vault_id):
        return 0

    def find_live_file_by_content_hash(self, owner_username, vault_id, content_hash):
        return None  # always a dedup miss -- push_req tests below drive the "needs data" path


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


class PlainServicer:
    """A servicer stand-in that only needs to satisfy _handle_init's read of
    max_file_size_bytes -- these tests never reach an actual RPC method."""

    max_file_size_bytes = 100


def make_authenticated_session(servicer):
    sender = RecordingSender()
    session = SyncWsSession(FakeRepository(), servicer, sender)
    session.handle_text_message(json.dumps(
        {"op": "init", "payload": {"token": "tok", "vaultId": "vault1"}}
    ))
    assert session.authenticated
    return session, sender


def send_text(session, op, payload):
    return session.handle_text_message(json.dumps({"op": op, "payload": payload}))


def run(coro):
    if coro is not None:
        asyncio.run(coro)


# ─── _file_chunk_to_frame: the oneof-unset guard ────────────────────────────────────────────


def test_file_chunk_to_frame_raises_for_a_chunk_with_no_payload_member_set():
    # header/data/eof are all None -- WhichOneof("payload") falls through to None, which
    # _file_chunk_to_frame treats as a hard error rather than silently doing nothing, since a
    # real DownloadFiles/DownloadHistoryVersion should never yield such a thing.
    with pytest.raises(ValueError, match="no payload set"):
        _file_chunk_to_frame(wire_types.FileChunk())


# ─── _run_request: servicer failures become an `error` op, not a crash ─────────────────────


class AbortingDeltaServicer(PlainServicer):
    async def Delta(self, request, context):
        context.abort(StatusCode.INVALID_ARGUMENT, "bad delta")


class CrashingDeltaServicer(PlainServicer):
    async def Delta(self, request, context):
        raise RuntimeError("delta boom")


def test_run_request_turns_a_ws_aborted_servicer_call_into_an_error_op():
    session, sender = make_authenticated_session(AbortingDeltaServicer())

    run(send_text(session, "delta_req", {"vaultId": "vault1", "localFiles": []}))

    assert sender.last == {
        "op": "error",
        "payload": {"code": "INVALID_ARGUMENT", "message": "bad delta"},
    }


def test_run_request_turns_an_unexpected_servicer_exception_into_an_internal_error_op():
    session, sender = make_authenticated_session(CrashingDeltaServicer())

    run(send_text(session, "delta_req", {"vaultId": "vault1", "localFiles": []}))

    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INTERNAL"
    assert "delta boom" in sender.last["payload"]["message"]


# ─── _run_stream_request: same, for streaming (download_req) ops ───────────────────────────


class AbortingDownloadServicer(PlainServicer):
    async def DownloadFiles(self, request, context):
        context.abort(StatusCode.NOT_FOUND, "vault gone")


class CrashingDownloadServicer(PlainServicer):
    async def DownloadFiles(self, request, context):
        raise RuntimeError("download boom")


def test_run_stream_request_turns_a_ws_aborted_servicer_call_into_an_error_op_and_frees_the_lock():
    session, sender = make_authenticated_session(AbortingDownloadServicer())

    run(send_text(session, "download_req", {"vaultId": "vault1", "paths": ["a.md"]}))

    assert sender.last == {
        "op": "error",
        "payload": {"code": "NOT_FOUND", "message": "vault gone"},
    }
    # the `finally:` in _run_stream_request must release the slot even on failure, or every
    # later streaming op on this connection would be wrongly rejected as STREAM_IN_PROGRESS.
    assert session._stream_active is False
    assert sender.find("stream_end") == []


def test_run_stream_request_turns_an_unexpected_servicer_exception_into_an_internal_error_op():
    session, sender = make_authenticated_session(CrashingDownloadServicer())

    run(send_text(session, "download_req", {"vaultId": "vault1", "paths": ["a.md"]}))

    assert sender.last["op"] == "error"
    assert sender.last["payload"]["code"] == "INTERNAL"
    assert "download boom" in sender.last["payload"]["message"]
    assert session._stream_active is False


# ─── _run_push: same, for push_req's finalize call (PR6) ───────────────────────────────────
#
# Unlike _run_request/_run_stream_request, _run_push has no separate `except WsAborted` branch
# -- finalize_pushed_file/finalize_deduped_file are plain synchronous methods, not RPC-style
# servicer methods invoked through a WsServicerContext, so they have no context.abort() to raise
# it from. Any unexpected exception (e.g. an unhandled disk error) becomes a generic INTERNAL
# error, same shape as every other op's catch-all.


class CrashingPushServicer(PlainServicer):
    def finalize_pushed_file(self, *args, **kwargs):
        raise RuntimeError("push boom")


def _run_single_push(session):
    coro = send_text(session, "push_req", {"vaultId": "vault1", "path": "a.md", "contentHash": "deadbeef", "sizeBytes": 3, "modifiedAtMs": 1})

    async def drive():
        task = asyncio.ensure_future(coro)
        await asyncio.sleep(0.01)
        session.handle_binary_frame(b"abc")
        await task

    asyncio.run(drive())


def test_run_push_turns_an_unexpected_finalize_exception_into_an_internal_error_op():
    session, sender = make_authenticated_session(CrashingPushServicer())

    _run_single_push(session)

    err = sender.find("error")[-1]
    assert err["payload"]["code"] == "INTERNAL"
    assert "push boom" in err["payload"]["message"]
    assert session._stream_active is False


def test_binary_frame_with_no_active_push_is_dropped_and_logged(caplog):
    session, sender = make_authenticated_session(PlainServicer())
    sent_before = len(sender.sent)  # init_ok/ready were already recorded by make_authenticated_session

    with caplog.at_level(logging.WARNING, logger="pumice_server.ws_sync_resource"):
        session.handle_binary_frame(b"stray-data")

    # Dropped silently on the wire -- only a server-side log, no error op sent to the client.
    assert len(sender.sent) == sent_before
    assert "no push_req waiting for data" in caplog.text


# ─── SyncWebSocketProtocol: the autobahn glue (onOpen/onMessage/onClose/init-timeout) ───────


class FakeDelayedCall:
    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def active(self):
        return not self._cancelled


class FakeReactor:
    def __init__(self):
        self.calls = []

    def callLater(self, delay, fn, *args, **kwargs):
        call = FakeDelayedCall(delay, fn)
        self.calls.append(call)
        return call


def make_protocol(repo, servicer, change_notifier=None):
    reactor = FakeReactor()
    protocol = SyncWebSocketProtocol(repo, servicer, change_notifier, reactor=reactor)
    return protocol, reactor


def test_on_open_creates_a_session_and_arms_the_init_timeout(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, reactor = make_protocol(repo, servicer)

    protocol.onOpen()

    assert isinstance(protocol._session, SyncWsSession)
    assert protocol._session.authenticated is False
    [call] = reactor.calls
    assert call.delay == INIT_TIMEOUT_SECONDS
    assert call.fn == protocol._on_init_timeout
    assert protocol._init_timeout_call is call


def test_on_message_dispatches_binary_and_text_frames_and_cancels_init_timeout_after_auth(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, reactor = make_protocol(repo, servicer)
    protocol.onOpen()
    sent = []
    protocol.sendMessage = lambda data, isBinary=False: sent.append((data, isBinary))
    init_timeout_call = reactor.calls[0]

    # Before auth, a message must NOT cancel the init timeout.
    protocol.onMessage(b'{"op":"ping","payload":{}}', False)
    assert init_timeout_call.active() is True

    # init authenticates synchronously (returns no coroutine) and disarms the init timeout.
    protocol.onMessage(json.dumps(
        {"op": "init", "payload": {"token": "tok", "vaultId": "vault1"}}
    ).encode("utf-8"), False)
    assert protocol._session.authenticated is True
    assert init_timeout_call.active() is False

    content = b"abc"
    content_hash = hashlib.sha256(content).hexdigest()

    async def finish():
        # push_req itself returns a coroutine (unlike init/ping, which resolve synchronously)
        # that onMessage must schedule via asyncio.ensure_future rather than run inline -- it
        # awaits the binary frame the isBinary branch below delivers via handle_binary_frame.
        protocol.onMessage(json.dumps(
            {"op": "push_req", "payload": {"vaultId": "vault1", "path": "a.md", "contentHash": content_hash, "sizeBytes": len(content), "modifiedAtMs": 1}}
        ).encode("utf-8"), False)
        for _ in range(200):
            ops_so_far = [json.loads(d)["op"] for d, is_binary in sent if not is_binary]
            if "push_res" in ops_so_far:
                break
            await asyncio.sleep(0.005)
        protocol.onMessage(content, True)  # isBinary branch: dispatched straight to handle_binary_frame
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(finish())

    ops = [json.loads(data)["op"] for data, is_binary in sent if not is_binary]
    assert "push_ack" in ops


def test_on_close_cancels_the_init_timeout_and_unsubscribes_from_change_notifications(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, reactor = make_protocol(repo, servicer)
    protocol.onOpen()
    unsub_calls = []
    protocol._unsubscribe = lambda: unsub_calls.append(True)

    protocol.onClose(True, 1000, "bye")

    assert reactor.calls[0].active() is False
    assert unsub_calls == [True]


def test_init_timeout_drops_the_connection_when_still_unauthenticated(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, _ = make_protocol(repo, servicer)
    protocol.onOpen()
    closed = []
    protocol.sendClose = lambda code=None, reason=None: closed.append((code, reason))

    protocol._on_init_timeout()

    assert closed == [(4001, "init timeout")]


def test_send_json_encodes_the_message_as_a_utf8_json_text_frame(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, _ = make_protocol(repo, servicer)
    calls = []
    protocol.sendMessage = lambda data, isBinary=False: calls.append((data, isBinary))

    protocol._send_json({"op": "pong", "payload": {"x": 1}})

    [(data, is_binary)] = calls
    assert is_binary is False
    assert json.loads(data.decode("utf-8")) == {"op": "pong", "payload": {"x": 1}}


def test_send_binary_forwards_raw_bytes_unmodified_as_a_binary_frame(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    protocol, _ = make_protocol(repo, servicer)
    calls = []
    protocol.sendMessage = lambda data, isBinary=False: calls.append((data, isBinary))

    protocol._send_binary(b"raw-bytes")

    assert calls == [(b"raw-bytes", True)]


# ─── make_ws_sync_resource ───────────────────────────────────────────────────────────────────


def test_make_ws_sync_resource_wires_the_factory_to_produce_connected_protocol_instances(tmp_path, repo):
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    change_notifier = object()  # only needs to be threaded through unchanged, not exercised here

    resource = make_ws_sync_resource(repo, servicer, change_notifier)

    assert isinstance(resource, WebSocketResource)
    protocol = resource._factory.protocol()
    assert isinstance(protocol, SyncWebSocketProtocol)
    assert protocol._repository is repo
    assert protocol._servicer is servicer
    assert protocol._change_notifier is change_notifier
