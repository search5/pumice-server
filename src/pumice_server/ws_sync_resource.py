"""WebSocket sync endpoint (/ws) -- a single persistent connection per vault that replaces
gRPC-Web's per-RPC HTTP requests and SSE's /watch with one op-based protocol, modeled on
Obsidian core's own built-in Sync plugin (analyzed from the shipped app, not copied -- see
#11_websocket_동기화_프로토콜_설계.md for the design and the legal review behind it).

Wire format: JSON text frames for control messages ({"op", "payload"}), raw binary frames for
file content. Auth happens via the first application-level message (op="init") rather than a WS
handshake header, for the same reason Obsidian core does it that way -- the browser WebSocket
API doesn't support custom headers.

Exactly one request may be outstanding on a connection at a time (matches Obsidian core's own
Sync client exactly -- see #14_옵시디언싱크_정렬_구현계획.md in the `pumice` repo): there is no
requestId in the envelope, and the server never needs to correlate a response to a request
beyond "reply to whatever's currently pending on this connection." An earlier version of this
protocol multiplexed requests via a requestId field; removed as dead complexity once analysis
showed Obsidian core doesn't do this either, and pumice's own client no longer sends more than
one request at a time.

SyncWsSession holds all the per-connection protocol logic and is deliberately independent of
autobahn/Twisted -- `send` is an injected callable rather than a call to a transport method,
so it's unit-testable without a real socket (see tests/test_ws_sync_session.py).

SyncWebSocketProtocol is the thin autobahn glue on top: it owns the actual socket, the
init-timeout timer, and wires onMessage/onClose to a SyncWsSession instance.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory
from autobahn.twisted.resource import WebSocketResource
from twisted.internet import reactor as _default_reactor

from . import wire_types
from .status_codes import StatusCode

logger = logging.getLogger(__name__)

# How long a connection is given to send its init message before being dropped.
INIT_TIMEOUT_SECONDS = 5.0

SendFn = Callable[[Dict[str, Any]], None]


class WsAborted(Exception):
    """Raised by WsServicerContext.abort() to unwind out of a servicer method while carrying
    the status code/details that should go into the error op sent back to the client."""

    def __init__(self, code: StatusCode, details: str):
        super().__init__(details)
        self.code = code
        self.details = details


class WsServicerContext:
    """Adapter that lets SyncServiceServicer's existing async methods run unmodified over a WS
    connection. Auth already happened once, in the init handshake (SyncWsSession) --
    invocation_metadata() re-synthesizes the equivalent of an Authorization/x-device-name/
    x-user-name header set from the session's already-verified identity, so
    _verify_vault_access's per-call token lookup (unchanged) still works exactly the same."""

    def __init__(self, session: "SyncWsSession"):
        self._session = session
        self.code = StatusCode.OK
        self.details: Optional[str] = None

    def invocation_metadata(self):
        return [
            ("authorization", f"Bearer {self._session.token}"),
            ("x-device-name", quote(self._session.device_name)),
            ("x-user-name", quote(self._session.user_name)),
        ]

    def abort(self, code: StatusCode, details: str):
        self.code = code
        self.details = details
        raise WsAborted(code, details)

    async def write(self, message) -> None:
        # Only DownloadFiles uses this (it calls context.write(...) internally rather than
        # yielding, unlike DownloadHistoryVersion -- see the two _STREAM_HANDLERS entries
        # below).
        self._session._emit_stream_frame(_file_chunk_to_frame(message))


# ─── payload <-> wire_types conversion ──────────────────────────────────────────────────────
# PR5 (pumice's 13_PR5_계획.md): every SyncServiceServicer method this module calls speaks
# wire_types dataclasses -- neither this file nor service.py has any protobuf/grpcio dependency
# left. See llm-wiki/ in this repo for the per-method conversion log.


def _file_meta_from_dict(d: dict) -> "wire_types.FileMeta":
    return wire_types.FileMeta(
        path=d.get("path", ""),
        modified_at_ms=d.get("modifiedAtMs", 0),
        size_bytes=d.get("sizeBytes", 0),
        content_hash=d.get("contentHash", ""),
        is_deleted=d.get("isDeleted", False),
    )


def _file_meta_to_dict(m: "wire_types.FileMeta") -> dict:
    return {
        "path": m.path,
        "modifiedAtMs": m.modified_at_ms,
        "sizeBytes": m.size_bytes,
        "contentHash": m.content_hash,
        "isDeleted": m.is_deleted,
    }


async def _handle_delta(session: "SyncWsSession", payload: dict):
    request = wire_types.DeltaRequest(
        vault_id=payload.get("vaultId", ""),
        local_files=[_file_meta_from_dict(f) for f in payload.get("localFiles", [])],
    )
    response = await session.servicer.Delta(request, WsServicerContext(session))
    return "delta_res", {
        "needUpload": list(response.need_upload),
        "needDownload": [_file_meta_to_dict(m) for m in response.need_download],
    }


async def _handle_history(session: "SyncWsSession", payload: dict):
    request = wire_types.HistoryRequest(vault_id=payload.get("vaultId", ""), path=payload.get("path", ""))
    response = await session.servicer.GetFileHistory(request, WsServicerContext(session))
    return "history_res", {
        "versions": [
            {
                "historyId": v.history_id,
                "modifiedAtMs": v.modified_at_ms,
                "sizeBytes": v.size_bytes,
                "contentHash": v.content_hash,
                "deviceName": v.device_name,
                "userName": v.user_name,
                "deleted": v.deleted,
                "relatedPath": v.related_path,
            }
            for v in response.versions
        ],
    }


async def _handle_restore(session: "SyncWsSession", payload: dict):
    request = wire_types.RestoreHistoryRequest(
        vault_id=payload.get("vaultId", ""),
        path=payload.get("path", ""),
        history_id=payload.get("historyId", 0),
    )
    response = await session.servicer.RestoreHistoryVersion(request, WsServicerContext(session))
    return "restore_res", {"ok": response.ok, "error": response.error}


async def _handle_size(session: "SyncWsSession", payload: dict):
    request = wire_types.SizeRequest(vault_id=payload.get("vaultId", ""))
    response = await session.servicer.GetSize(request, WsServicerContext(session))
    return "size_res", {
        "vaultSizeBytes": response.vault_size_bytes,
        "totalSizeBytes": response.total_size_bytes,
        "limitBytes": response.limit_bytes,
    }


async def _handle_purge(session: "SyncWsSession", payload: dict):
    request = wire_types.PurgeRequest(vault_id=payload.get("vaultId", ""))
    response = await session.servicer.PurgeVault(request, WsServicerContext(session))
    return "purge_res", {"ok": response.ok, "error": response.error}


async def _handle_usernames(session: "SyncWsSession", payload: dict):
    request = wire_types.UsernamesRequest(vault_id=payload.get("vaultId", ""))
    response = await session.servicer.GetUsernames(request, WsServicerContext(session))
    return "usernames_res", {"usernames": response.usernames}


# Ops that need an async servicer call, dispatched after authentication. init/ping are handled
# synchronously by SyncWsSession itself -- see handle_text_message.
_REQUEST_HANDLERS: Dict[str, Callable] = {
    "delta_req": _handle_delta,
    "history_req": _handle_history,
    "restore_req": _handle_restore,
    "size_req": _handle_size,
    "purge_req": _handle_purge,
    "usernames_req": _handle_usernames,
}


# ─── binary file-chunk streaming (upload/download/history-download) ────────────────────────
#
# Raw binary WS frames carry no envelope at all (matching Obsidian core's framing), so only one
# streaming op may be active on a connection at a time -- see SyncWsSession._stream_active. In
# practice a well-behaved client never attempts a second one anyway (see the module docstring),
# but the server still rejects an overlapping attempt defensively.


def _file_chunk_to_frame(chunk: "wire_types.FileChunk"):
    """Translates one FileChunk (as already yielded by DownloadFiles/DownloadHistoryVersion,
    unmodified) into a frame to send: ("json", op, payload) for header/eof, or ("binary", bytes)
    for the raw file data -- the data itself, not wrapped in any envelope."""
    which = chunk.WhichOneof("payload")
    if which == "header":
        h = chunk.header
        return ("json", "file_chunk_header", {
            "vaultId": h.vault_id, "path": h.path, "totalBytes": h.total_bytes, "modifiedAtMs": h.modified_at_ms,
        })
    if which == "data":
        return ("binary", chunk.data.data)
    if which == "eof":
        e = chunk.eof
        return ("json", "file_chunk_eof", {"path": e.path, "contentHash": e.content_hash})
    raise ValueError("FileChunk with no payload set")


async def _handle_download(session: "SyncWsSession", payload: dict) -> None:
    # DownloadFiles calls context.write(...) internally for each chunk rather than yielding
    # (see service.py -- it fans the batch out across concurrent per-file tasks, so a plain
    # generator wouldn't preserve per-file ordering the way DownloadHistoryVersion's does).
    # WsServicerContext.write() forwards each one straight to _emit_stream_frame as it happens.
    request = wire_types.DownloadBatchRequest(vault_id=payload.get("vaultId", ""), paths=payload.get("paths", []))
    await session.servicer.DownloadFiles(request, WsServicerContext(session))


async def _stream_history_download(session: "SyncWsSession", payload: dict):
    request = wire_types.HistoryVersionDownloadRequest(
        vault_id=payload.get("vaultId", ""), path=payload.get("path", ""), history_id=payload.get("historyId", 0),
    )
    async for chunk in session.servicer.DownloadHistoryVersion(request, WsServicerContext(session)):
        yield _file_chunk_to_frame(chunk)


# Ops whose response is a stream of frames (JSON control + raw binary) rather than one JSON
# response -- dispatched through _run_stream_request instead of _run_request. download_req's
# handler emits its own frames via WsServicerContext.write() as DownloadFiles calls it (so it's
# a plain coroutine, not a generator); the others are async generators consumed with `async for`.
_WRITE_BASED_STREAM_OPS = {"download_req"}
_STREAM_HANDLERS: Dict[str, Callable] = {
    "download_req": _handle_download,
    "history_dl_req": _stream_history_download,
}


class SyncWsSession:
    def __init__(self, repository, servicer, send: SendFn, send_binary: Optional[Callable[[bytes], None]] = None,
                 on_authenticated: Optional[Callable[[str, str], None]] = None):
        self.repository = repository
        self.servicer = servicer
        self._send = send
        self._send_binary = send_binary
        self._on_authenticated = on_authenticated
        self.authenticated = False
        self.owner_username: Optional[str] = None
        self.vault_id: Optional[str] = None
        self.device_name: str = ""
        self.user_name: str = ""
        self.token: str = ""

        # Streaming (binary file-chunk) op state -- exactly one of upload/download/history-
        # download may be active on a connection at a time (see _try_claim_stream). For push_req
        # (PR6 of #14_옵시디언싱크_정렬_구현계획.md), _push_data_future is how the async
        # _run_push() coroutine awaits the next raw binary WS frame: handle_binary_frame() is
        # invoked synchronously by the autobahn protocol's onMessage, with no coroutine of its
        # own to hand the bytes to directly, so it just resolves this future if one is waiting.
        self._stream_active: bool = False
        self._push_data_future: Optional["asyncio.Future"] = None

    def handle_text_message(self, raw: str) -> Optional["asyncio.Future"]:
        """Handles one text frame. init/ping/parse-errors are resolved synchronously (return
        None). An op needing an async servicer call (or, for push_req, a round trip to the
        client for binary data) returns a coroutine the caller must schedule (e.g.
        asyncio.ensure_future) -- kept as a plain coroutine rather than being scheduled in here
        so this stays synchronously testable without a running event loop."""
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            self._send_error("INVALID_JSON", "Malformed JSON message")
            return None

        op = envelope.get("op")
        payload = envelope.get("payload") or {}

        if not self.authenticated:
            if op != "init":
                self._send_error("UNAUTHENTICATED", "Must send init first")
                return None
            self._handle_init(payload)
            return None

        if op == "ping":
            self._send_pong()
            return None

        if op == "push_req":
            # Claimed synchronously (not inside the coroutine below) so a second streaming op
            # is rejected immediately, before any scheduling -- same as every other stream op.
            if not self._try_claim_stream():
                return None
            return self._run_push(payload)

        handler = _REQUEST_HANDLERS.get(op)
        if handler is not None:
            return self._run_request(op, handler, payload)

        stream_handler = _STREAM_HANDLERS.get(op)
        if stream_handler is not None:
            # Claimed synchronously (not inside the coroutine below) so a second streaming op
            # is rejected immediately, before any scheduling -- same as upload_begin's claim.
            if not self._try_claim_stream():
                return None
            return self._run_stream_request(op, stream_handler, payload)

        self._send_error("UNKNOWN_OP", f"Unknown op: {op}")
        return None

    async def _run_request(self, op: str, handler: Callable, payload: dict) -> None:
        try:
            response_op, response_payload = await handler(self, payload)
        except WsAborted as e:
            self._send_error(e.code.name, e.details or "")
        except Exception as e:
            logger.error(f"WS op '{op}' failed: {e}")
            self._send_error("INTERNAL", str(e))
        else:
            self._send({"op": response_op, "payload": response_payload})

    # ─── streaming (binary file-chunk) ops ──────────────────────────────────────────────────

    def _try_claim_stream(self) -> bool:
        if self._stream_active:
            self._send_error("STREAM_IN_PROGRESS", "Another file stream is already active on this connection")
            return False
        self._stream_active = True
        return True

    def _emit_stream_frame(self, frame) -> None:
        if frame[0] == "json":
            _, frame_op, frame_payload = frame
            self._send({"op": frame_op, "payload": frame_payload})
        else:
            assert self._send_binary is not None, "send_binary must be provided for streaming ops"
            self._send_binary(frame[1])

    async def _run_stream_request(self, op: str, handler: Callable, payload: dict) -> None:
        # Claim already happened synchronously in handle_text_message before this coroutine was
        # even created -- see the stream_handler dispatch branch there.
        try:
            if op in _WRITE_BASED_STREAM_OPS:
                await handler(self, payload)
            else:
                async for frame in handler(self, payload):
                    self._emit_stream_frame(frame)
        except WsAborted as e:
            self._send_error(e.code.name, e.details or "")
        except Exception as e:
            logger.error(f"WS stream op '{op}' failed: {e}")
            self._send_error("INTERNAL", str(e))
        else:
            # Explicit terminal signal -- a download_req/history_dl_req batch can silently skip
            # paths that don't exist server-side (DownloadFiles just returns early for those,
            # no ack), so the client can't reliably infer "done" just by counting per-file
            # header/eof pairs against what it asked for. Only sent on success: an error frame
            # above already tells the client the stream is finished.
            self._send({"op": "stream_end", "payload": {}})
        finally:
            self._stream_active = False

    def handle_binary_frame(self, data: bytes) -> None:
        if self._push_data_future is not None and not self._push_data_future.done():
            self._push_data_future.set_result(data)
            self._push_data_future = None
        else:
            logger.warning("WS: received a binary frame with no push_req waiting for data")

    async def _run_push(self, payload: dict) -> None:
        """PR6 of #14_옵시디언싱크_정렬_구현계획.md: one push_req end to end. Content-addressed
        dedup (mirrors real Obsidian Sync's own `push` op, confirmed via obsidian.asar analysis)
        -- if the declared contentHash already matches a live file elsewhere in this vault, the
        server hard-links/copies it onto the new path and skips the byte transfer entirely
        (push_ack with needData=false, no push_res, no binary frame). Otherwise push_res tells
        the client to send exactly one raw binary frame with the whole file, which
        finalize_pushed_file() verifies and writes."""
        vault_id = payload.get("vaultId", self.vault_id or "")
        rel_path = payload.get("path", "")
        content_hash = payload.get("contentHash", "")
        size_bytes = payload.get("sizeBytes", 0)
        modified_at_ms = payload.get("modifiedAtMs", 0)
        try:
            # perFileMax (see #11_websocket_동기화_프로토콜_설계.md's re-analysis), advertised in
            # init_ok -- rejected before any dedup lookup or disk work, same as the now-removed
            # UploadFiles did, so a client never wastes a round trip sending bytes the server was
            # always going to refuse.
            if size_bytes > self.servicer.max_file_size_bytes:
                self._send({"op": "push_ack", "payload": {"ok": False, "needData": False, "error": "File too large"}})
                return
            dedup_source = await asyncio.to_thread(
                self.repository.find_live_file_by_content_hash, self.owner_username, vault_id, content_hash
            )
            if dedup_source is not None and dedup_source["path"] != rel_path:
                await asyncio.to_thread(
                    self.servicer.finalize_deduped_file, self.owner_username, vault_id, rel_path,
                    dedup_source, modified_at_ms, self.device_name, self.user_name,
                )
                self._send({"op": "push_ack", "payload": {"ok": True, "needData": False, "error": ""}})
            else:
                self._send({"op": "push_res", "payload": {"needData": True}})
                self._push_data_future = asyncio.get_event_loop().create_future()
                data = await self._push_data_future
                ok, err = await asyncio.to_thread(
                    self.servicer.finalize_pushed_file, self.owner_username, vault_id, rel_path,
                    data, size_bytes, modified_at_ms, content_hash, self.device_name, self.user_name,
                )
                self._send({"op": "push_ack", "payload": {"ok": ok, "needData": True, "error": err}})
        except Exception as e:
            logger.error(f"WS push failed for {rel_path}: {e}")
            self._send_error("INTERNAL", str(e))
        finally:
            self._push_data_future = None
            self._send({"op": "stream_end", "payload": {}})
            self._stream_active = False

    def _handle_init(self, payload: dict) -> None:
        token = payload.get("token", "")
        vault_id = payload.get("vaultId", "")
        if not token or not vault_id:
            self._send_error("INVALID_ARGUMENT", "token and vaultId are required")
            return

        device = self.repository.get_device_token(token)
        if not device:
            self._send_error("UNAUTHENTICATED", "Invalid or missing auth token")
            return

        self.authenticated = True
        self.owner_username = device["username"]
        self.vault_id = vault_id
        self.device_name = payload.get("deviceName", "")
        self.user_name = payload.get("userName", "")
        self.token = token

        self._send({
            "op": "init_ok",
            "payload": {
                "serverVersion": "0.1.0",
                "timestampMs": int(time.time() * 1000),
                "maxFileSizeBytes": self.servicer.max_file_size_bytes,
            },
        })

        # Version-catch-up (PR2 of #14_옵시디언싱크_정렬_구현계획.md, pumice repo) -- matches
        # Obsidian core's own Sync client exactly (confirmed via obsidian.asar analysis): there
        # is no separate "give me changes since X" RPC. The client reports its last known
        # version in `init`, and the server just streams the intervening changes as ordinary
        # `push` frames (the exact same op/shape the client already handles for live updates),
        # then signals `ready`. last_known_change_id=0 (a brand-new device with no baseline)
        # deliberately skips the catch-up burst -- see the design doc for why: it'd mean
        # replaying potentially thousands of files as individual push frames instead of letting
        # the client's existing delta_req full-scan bootstrap handle it in one round trip.
        last_known_change_id = payload.get("lastKnownChangeId", 0)
        if last_known_change_id > 0:
            changes = self.repository.get_changes_since(self.owner_username, vault_id, last_known_change_id)
            for file_meta in changes:
                self._send({
                    "op": "push",
                    "payload": {
                        "vaultId": vault_id,
                        "path": file_meta["path"],
                        "modifiedAtMs": file_meta["modified_at_ms"],
                        "sizeBytes": file_meta["size_bytes"],
                        "contentHash": file_meta["content_hash"],
                        "isDeleted": file_meta["is_deleted"],
                    },
                })

        latest_change_id = self.repository.get_latest_change_id(self.owner_username, vault_id)
        self._send({"op": "ready", "payload": {"latestChangeId": latest_change_id}})

        if self._on_authenticated:
            self._on_authenticated(self.owner_username, self.vault_id)

    def _send_pong(self) -> None:
        self._send({
            "op": "pong",
            "payload": {"timestampMs": int(time.time() * 1000)},
        })

    def _send_error(self, code: str, message: str) -> None:
        self._send({
            "op": "error",
            "payload": {"code": code, "message": message},
        })


class SyncWebSocketProtocol(WebSocketServerProtocol):
    def __init__(self, repository, servicer, change_notifier=None, reactor=None):
        super().__init__()
        self._repository = repository
        self._servicer = servicer
        self._change_notifier = change_notifier
        self._reactor = reactor if reactor is not None else _default_reactor
        self._session: Optional[SyncWsSession] = None
        self._init_timeout_call = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    def onOpen(self):
        self._session = SyncWsSession(self._repository, self._servicer, send=self._send_json,
                                       send_binary=self._send_binary, on_authenticated=self._on_authenticated)
        self._init_timeout_call = self._reactor.callLater(INIT_TIMEOUT_SECONDS, self._on_init_timeout)

    def onMessage(self, payload, isBinary):
        assert self._session is not None
        if isBinary:
            self._session.handle_binary_frame(payload)
            return

        coro = self._session.handle_text_message(payload.decode("utf-8"))
        if coro is not None:
            asyncio.ensure_future(coro)

        if self._session.authenticated and self._init_timeout_call and self._init_timeout_call.active():
            self._init_timeout_call.cancel()

    def onClose(self, wasClean, code, reason):
        if self._init_timeout_call and self._init_timeout_call.active():
            self._init_timeout_call.cancel()
        if self._unsubscribe is not None:
            self._unsubscribe()

    def _on_authenticated(self, owner_username: str, vault_id: str) -> None:
        if self._change_notifier is not None:
            self._unsubscribe = self._change_notifier.subscribe(owner_username, vault_id, self._on_change)

    def _on_change(self, file_meta: dict) -> None:
        assert self._session is not None
        self._send_json({
            "op": "push",
            "payload": {
                "vaultId": self._session.vault_id,
                "path": file_meta["path"],
                "modifiedAtMs": file_meta["modified_at_ms"],
                "sizeBytes": file_meta["size_bytes"],
                "contentHash": file_meta["content_hash"],
                "isDeleted": file_meta["is_deleted"],
            },
        })

    def _on_init_timeout(self):
        if not self._session or not self._session.authenticated:
            logger.info("WS connection dropped: no init within %.0fs", INIT_TIMEOUT_SECONDS)
            self.sendClose(code=4001, reason="init timeout")

    def _send_json(self, message: Dict[str, Any]) -> None:
        self.sendMessage(json.dumps(message).encode("utf-8"), isBinary=False)

    def _send_binary(self, data: bytes) -> None:
        self.sendMessage(data, isBinary=True)


def make_ws_sync_resource(repository, servicer, change_notifier=None) -> WebSocketResource:
    factory = WebSocketServerFactory()
    factory.protocol = lambda: SyncWebSocketProtocol(repository, servicer, change_notifier)
    return WebSocketResource(factory)
