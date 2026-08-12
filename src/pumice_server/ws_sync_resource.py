"""WebSocket sync endpoint (/ws) -- a single persistent connection per vault that replaces
gRPC-Web's per-RPC HTTP requests and SSE's /watch with one op-based protocol, modeled on
Obsidian core's own built-in Sync plugin (analyzed from the shipped app, not copied -- see
#11_websocket_동기화_프로토콜_설계.md for the design and the legal review behind it).

Wire format: JSON text frames for control messages ({"requestId", "op", "payload"}), raw
binary frames for file content (added in a later PR alongside upload/download op handling).
Auth happens via the first application-level message (op="init") rather than a WS handshake
header, for the same reason Obsidian core does it that way -- the browser WebSocket API
doesn't support custom headers.

SyncWsSession holds all the per-connection protocol logic and is deliberately independent of
autobahn/Twisted -- `send` is an injected callable rather than a call to a transport method,
so it's unit-testable without a real socket (see tests/test_ws_sync_session.py). This mirrors
NativeServicerContext/WsServicerContext's split in grpc_web_resource.py between transport
plumbing and business logic.

SyncWebSocketProtocol is the thin autobahn glue on top: it owns the actual socket, the
init-timeout timer, and wires onMessage/onClose to a SyncWsSession instance.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import grpc
from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory
from autobahn.twisted.resource import WebSocketResource
from twisted.internet import reactor as _default_reactor

from . import sync_pb2
from . import wire_types

logger = logging.getLogger(__name__)

# How long a connection is given to send its init message before being dropped.
INIT_TIMEOUT_SECONDS = 5.0

# Advertised to the client in init_ok -- bounds how many requestIds it may keep in flight at
# once on this connection (see #11 설계 문서, "요청 동시성" 절).
MAX_IN_FLIGHT = 8

SendFn = Callable[[Dict[str, Any]], None]


class WsAborted(Exception):
    """Raised by WsServicerContext.abort(), mirroring grpc_web_resource.py's Aborted -- kept as
    a separate class (not imported from there) so this module has no dependency on
    grpc_web_resource.py, which is slated for removal once the gRPC-Web transport is retired."""

    def __init__(self, code: grpc.StatusCode, details: str):
        super().__init__(details)
        self.code = code
        self.details = details


class WsServicerContext:
    """grpc.ServicerContext-shaped adapter that lets SyncServiceServicer's existing async
    methods run completely unmodified over a WS connection. Auth already happened once, in the
    init handshake (SyncWsSession) -- invocation_metadata() re-synthesizes the equivalent of the
    old Authorization/x-device-name/x-user-name headers from the session's already-verified
    identity so _verify_vault_access's per-call token lookup (unchanged) still works exactly as
    it does for gRPC-Web."""

    def __init__(self, session: "SyncWsSession"):
        self._session = session
        self.code = grpc.StatusCode.OK
        self.details: Optional[str] = None

    def invocation_metadata(self):
        return [
            ("authorization", f"Bearer {self._session.token}"),
            ("x-device-name", quote(self._session.device_name)),
            ("x-user-name", quote(self._session.user_name)),
        ]

    def abort(self, code: grpc.StatusCode, details: str):
        self.code = code
        self.details = details
        raise WsAborted(code, details)

    async def write(self, message) -> None:
        # Only DownloadFiles uses this (it calls context.write(...) internally rather than
        # yielding, unlike DownloadHistoryVersion -- see the two _STREAM_HANDLERS entries
        # below). self._session._active_stream_request_id is set for the whole duration of the
        # call that owns this context, so it's always the right requestId to tag frames with.
        self._session._emit_stream_frame(self._session._active_stream_request_id, _file_chunk_to_frame(message))


# ─── payload <-> protobuf conversion (temporary boundary layer -- see PR2 note in
# #11_websocket_동기화_프로토콜_설계.md: SyncServiceServicer keeps speaking protobuf internally
# until sync.proto itself is removed in the final PR; only the WS edge translates JSON <-> pb) ──


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
    request = sync_pb2.HistoryRequest(vault_id=payload.get("vaultId", ""), path=payload.get("path", ""))
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
            }
            for v in response.versions
        ],
    }


async def _handle_restore(session: "SyncWsSession", payload: dict):
    request = sync_pb2.RestoreHistoryRequest(
        vault_id=payload.get("vaultId", ""),
        path=payload.get("path", ""),
        history_id=payload.get("historyId", 0),
    )
    response = await session.servicer.RestoreHistoryVersion(request, WsServicerContext(session))
    return "restore_res", {"ok": response.ok, "error": response.error}


# Ops that need an async servicer call, dispatched after authentication. init/ping are handled
# synchronously by SyncWsSession itself -- see handle_text_message.
_REQUEST_HANDLERS: Dict[str, Callable] = {
    "delta_req": _handle_delta,
    "history_req": _handle_history,
    "restore_req": _handle_restore,
}


# ─── binary file-chunk streaming (upload/download/history-download) ────────────────────────
#
# Raw binary WS frames carry no requestId envelope (matching Obsidian core's framing), so only
# one streaming op may be active on a connection at a time -- see SyncWsSession's
# _active_stream_request_id. Non-streaming ops above are unaffected and stay fully multiplexed.


def _file_chunk_to_frame(chunk: "sync_pb2.FileChunk"):
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
    request = sync_pb2.DownloadBatchRequest(vault_id=payload.get("vaultId", ""), paths=payload.get("paths", []))
    await session.servicer.DownloadFiles(request, WsServicerContext(session))


async def _stream_history_download(session: "SyncWsSession", payload: dict):
    request = sync_pb2.HistoryVersionDownloadRequest(
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


class _UploadAccumulator:
    """Buffers incoming file_chunk_header/binary/file_chunk_eof frames for one upload_begin
    batch into FileChunk protos -- exactly the shape UploadBatch.chunks already has for the
    existing (unmodified) UploadFiles RPC. The whole batch is handed to UploadFiles() only once
    every declared file has its EOF (see SyncWsSession._finish_upload). Real incremental
    (constant-memory) upload streaming, mirroring streaming_upload_resource.py's per-chunk
    finalize, is a possible future optimization -- out of scope here, and not a regression
    since UploadFiles's existing gRPC-Web path already fully buffers a batch today too."""

    def __init__(self, vault_id: str, file_count: int):
        self.vault_id = vault_id
        self.file_count = file_count
        self.chunks: list = []
        self.files_completed = 0

    def add_header(self, path: str, total_bytes: int, modified_at_ms: int) -> None:
        self.chunks.append(sync_pb2.FileChunk(header=sync_pb2.ChunkHeader(
            vault_id=self.vault_id, path=path, total_bytes=total_bytes, modified_at_ms=modified_at_ms,
        )))

    def add_data(self, path: str, sequence: int, data: bytes) -> None:
        self.chunks.append(sync_pb2.FileChunk(data=sync_pb2.ChunkData(path=path, sequence=sequence, data=data)))

    def add_eof(self, path: str, content_hash: str) -> None:
        self.chunks.append(sync_pb2.FileChunk(eof=sync_pb2.ChunkEOF(path=path, content_hash=content_hash)))
        self.files_completed += 1

    @property
    def is_complete(self) -> bool:
        return self.files_completed >= self.file_count


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

        # Streaming (binary file-chunk) op state -- see the module-level note above
        # _UploadAccumulator for why only one of these can be active at a time.
        self._active_stream_request_id: Optional[int] = None
        self._upload_accumulator: Optional[_UploadAccumulator] = None
        self._upload_current_path: Optional[str] = None
        self._upload_current_sequence: int = 0

    def handle_text_message(self, raw: str) -> Optional["asyncio.Future"]:
        """Handles one text frame. init/ping/parse-errors/upload-chunk-control are resolved
        synchronously (return None). An op needing an async servicer call returns a coroutine
        the caller must schedule (e.g. asyncio.ensure_future) -- kept as a plain coroutine
        rather than being scheduled in here so this stays synchronously testable without a
        running event loop. file_chunk_eof is the one exception that *may* return a coroutine
        (when it completes an upload batch, triggering UploadFiles)."""
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            self._send_error(0, "INVALID_JSON", "Malformed JSON message")
            return None

        request_id = envelope.get("requestId", 0)
        op = envelope.get("op")
        payload = envelope.get("payload") or {}

        if not self.authenticated:
            if op != "init":
                self._send_error(request_id, "UNAUTHENTICATED", "Must send init first")
                return None
            self._handle_init(request_id, payload)
            return None

        if op == "ping":
            self._send_pong(request_id)
            return None

        if op == "upload_begin":
            self._handle_upload_begin(request_id, payload)
            return None
        if op == "file_chunk_header":
            self._handle_file_chunk_header(request_id, payload)
            return None
        if op == "file_chunk_eof":
            return self._handle_file_chunk_eof(request_id, payload)

        handler = _REQUEST_HANDLERS.get(op)
        if handler is not None:
            return self._run_request(request_id, op, handler, payload)

        stream_handler = _STREAM_HANDLERS.get(op)
        if stream_handler is not None:
            # Claimed synchronously (not inside the coroutine below) so a second streaming op
            # is rejected immediately, before any scheduling -- same as upload_begin's claim.
            if not self._try_claim_stream(request_id):
                return None
            return self._run_stream_request(request_id, op, stream_handler, payload)

        self._send_error(request_id, "UNKNOWN_OP", f"Unknown op: {op}")
        return None

    async def _run_request(self, request_id: int, op: str, handler: Callable, payload: dict) -> None:
        try:
            response_op, response_payload = await handler(self, payload)
        except WsAborted as e:
            self._send_error(request_id, e.code.name, e.details or "")
        except Exception as e:
            logger.error(f"WS op '{op}' failed: {e}")
            self._send_error(request_id, "INTERNAL", str(e))
        else:
            self._send({"requestId": request_id, "op": response_op, "payload": response_payload})

    # ─── streaming (binary file-chunk) ops ──────────────────────────────────────────────────

    def _try_claim_stream(self, request_id: int) -> bool:
        if self._active_stream_request_id is not None:
            self._send_error(request_id, "STREAM_IN_PROGRESS", "Another file stream is already active on this connection")
            return False
        self._active_stream_request_id = request_id
        return True

    def _emit_stream_frame(self, request_id: int, frame) -> None:
        if frame[0] == "json":
            _, frame_op, frame_payload = frame
            self._send({"requestId": request_id, "op": frame_op, "payload": frame_payload})
        else:
            assert self._send_binary is not None, "send_binary must be provided for streaming ops"
            self._send_binary(frame[1])

    async def _run_stream_request(self, request_id: int, op: str, handler: Callable, payload: dict) -> None:
        # Claim already happened synchronously in handle_text_message before this coroutine was
        # even created -- see the stream_handler dispatch branch there.
        try:
            if op in _WRITE_BASED_STREAM_OPS:
                await handler(self, payload)
            else:
                async for frame in handler(self, payload):
                    self._emit_stream_frame(request_id, frame)
        except WsAborted as e:
            self._send_error(request_id, e.code.name, e.details or "")
        except Exception as e:
            logger.error(f"WS stream op '{op}' failed: {e}")
            self._send_error(request_id, "INTERNAL", str(e))
        else:
            # Explicit terminal signal -- a download_req/history_dl_req batch can silently skip
            # paths that don't exist server-side (DownloadFiles just returns early for those,
            # no ack), so the client can't reliably infer "done" just by counting per-file
            # header/eof pairs against what it asked for. Only sent on success: an error frame
            # above already tells the client the requestId is finished.
            self._send({"requestId": request_id, "op": "stream_end", "payload": {}})
        finally:
            self._active_stream_request_id = None

    def _handle_upload_begin(self, request_id: int, payload: dict) -> None:
        if not self._try_claim_stream(request_id):
            return
        vault_id = payload.get("vaultId", self.vault_id or "")
        file_count = payload.get("fileCount", 0)
        self._upload_accumulator = _UploadAccumulator(vault_id, file_count)
        self._upload_current_path = None
        self._upload_current_sequence = 0

    def _handle_file_chunk_header(self, request_id: int, payload: dict) -> None:
        if self._active_stream_request_id != request_id or self._upload_accumulator is None:
            self._send_error(request_id, "INVALID_STATE", "file_chunk_header without an active upload_begin")
            return
        path = payload.get("path", "")
        self._upload_accumulator.add_header(path, payload.get("totalBytes", 0), payload.get("modifiedAtMs", 0))
        self._upload_current_path = path
        self._upload_current_sequence = 0

    def handle_binary_frame(self, data: bytes) -> None:
        if self._active_stream_request_id is None or self._upload_accumulator is None or self._upload_current_path is None:
            logger.warning("WS: received a binary frame with no active upload in progress")
            return
        self._upload_accumulator.add_data(self._upload_current_path, self._upload_current_sequence, data)
        self._upload_current_sequence += 1

    def _handle_file_chunk_eof(self, request_id: int, payload: dict) -> Optional["asyncio.Future"]:
        if self._active_stream_request_id != request_id or self._upload_accumulator is None:
            self._send_error(request_id, "INVALID_STATE", "file_chunk_eof without an active upload_begin")
            return None
        path = payload.get("path", "")
        self._upload_accumulator.add_eof(path, payload.get("contentHash", ""))
        self._upload_current_path = None

        if not self._upload_accumulator.is_complete:
            return None

        accumulator = self._upload_accumulator
        self._upload_accumulator = None
        return self._finish_upload(request_id, accumulator)

    async def _finish_upload(self, request_id: int, accumulator: _UploadAccumulator) -> None:
        try:
            batch = sync_pb2.UploadBatch(chunks=accumulator.chunks)
            async for ack in self.servicer.UploadFiles(batch, WsServicerContext(self)):
                self._send({
                    "requestId": request_id,
                    "op": "upload_ack",
                    "payload": {"path": ack.path, "ok": ack.ok, "error": ack.error},
                })
        except WsAborted as e:
            self._send_error(request_id, e.code.name, e.details or "")
        except Exception as e:
            logger.error(f"WS upload (request {request_id}) failed: {e}")
            self._send_error(request_id, "INTERNAL", str(e))
        else:
            # Same terminal signal as the download/history-download streams (see
            # _run_stream_request) -- keeps the client's "how do I know this requestId is done"
            # logic uniform across every streaming op instead of needing upload-specific
            # ack-counting against fileCount.
            self._send({"requestId": request_id, "op": "stream_end", "payload": {}})
        finally:
            self._active_stream_request_id = None

    def _handle_init(self, request_id: int, payload: dict) -> None:
        token = payload.get("token", "")
        vault_id = payload.get("vaultId", "")
        if not token or not vault_id:
            self._send_error(request_id, "INVALID_ARGUMENT", "token and vaultId are required")
            return

        device = self.repository.get_device_token(token)
        if not device:
            self._send_error(request_id, "UNAUTHENTICATED", "Invalid or missing auth token")
            return

        self.authenticated = True
        self.owner_username = device["username"]
        self.vault_id = vault_id
        self.device_name = payload.get("deviceName", "")
        self.user_name = payload.get("userName", "")
        self.token = token

        self._send({
            "requestId": request_id,
            "op": "init_ok",
            "payload": {
                "serverVersion": "0.1.0",
                "timestampMs": int(time.time() * 1000),
                "maxInFlight": MAX_IN_FLIGHT,
            },
        })

        if self._on_authenticated:
            self._on_authenticated(self.owner_username, self.vault_id)

    def _send_pong(self, request_id: int) -> None:
        self._send({
            "requestId": request_id,
            "op": "pong",
            "payload": {"timestampMs": int(time.time() * 1000)},
        })

    def _send_error(self, request_id: int, code: str, message: str) -> None:
        self._send({
            "requestId": request_id,
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

    def _on_change(self) -> None:
        assert self._session is not None
        self._send_json({"requestId": 0, "op": "push", "payload": {"vaultId": self._session.vault_id}})

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
