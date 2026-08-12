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


# ─── payload <-> protobuf conversion (temporary boundary layer -- see PR2 note in
# #11_websocket_동기화_프로토콜_설계.md: SyncServiceServicer keeps speaking protobuf internally
# until sync.proto itself is removed in the final PR; only the WS edge translates JSON <-> pb) ──


def _file_meta_from_dict(d: dict) -> "sync_pb2.FileMeta":
    return sync_pb2.FileMeta(
        path=d.get("path", ""),
        modified_at_ms=d.get("modifiedAtMs", 0),
        size_bytes=d.get("sizeBytes", 0),
        content_hash=d.get("contentHash", ""),
        is_deleted=d.get("isDeleted", False),
    )


def _file_meta_to_dict(m: "sync_pb2.FileMeta") -> dict:
    return {
        "path": m.path,
        "modifiedAtMs": m.modified_at_ms,
        "sizeBytes": m.size_bytes,
        "contentHash": m.content_hash,
        "isDeleted": m.is_deleted,
    }


async def _handle_delta(session: "SyncWsSession", payload: dict):
    request = sync_pb2.DeltaRequest(
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


class SyncWsSession:
    def __init__(self, repository, servicer, send: SendFn, on_authenticated: Optional[Callable[[str, str], None]] = None):
        self.repository = repository
        self.servicer = servicer
        self._send = send
        self._on_authenticated = on_authenticated
        self.authenticated = False
        self.owner_username: Optional[str] = None
        self.vault_id: Optional[str] = None
        self.device_name: str = ""
        self.user_name: str = ""
        self.token: str = ""

    def handle_text_message(self, raw: str) -> Optional["asyncio.Future"]:
        """Handles one text frame. init/ping/parse-errors are resolved synchronously (return
        None). An RPC op (once authenticated) returns a coroutine the caller must schedule
        (e.g. asyncio.ensure_future) -- kept as a plain coroutine rather than being scheduled
        in here so this stays synchronously testable without a running event loop."""
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

        handler = _REQUEST_HANDLERS.get(op)
        if handler is None:
            self._send_error(request_id, "UNKNOWN_OP", f"Unknown op: {op}")
            return None

        return self._run_request(request_id, op, handler, payload)

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
                                       on_authenticated=self._on_authenticated)
        self._init_timeout_call = self._reactor.callLater(INIT_TIMEOUT_SECONDS, self._on_init_timeout)

    def onMessage(self, payload, isBinary):
        if isBinary:
            # File-chunk binary frames are handled once upload/download ops land (a later PR).
            return

        assert self._session is not None
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


def make_ws_sync_resource(repository, servicer, change_notifier=None) -> WebSocketResource:
    factory = WebSocketServerFactory()
    factory.protocol = lambda: SyncWebSocketProtocol(repository, servicer, change_notifier)
    return WebSocketResource(factory)
