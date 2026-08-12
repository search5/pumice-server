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

import json
import logging
import time
from typing import Any, Callable, Dict, Optional

from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory
from autobahn.twisted.resource import WebSocketResource
from twisted.internet import reactor as _default_reactor

logger = logging.getLogger(__name__)

# How long a connection is given to send its init message before being dropped.
INIT_TIMEOUT_SECONDS = 5.0

# Advertised to the client in init_ok -- bounds how many requestIds it may keep in flight at
# once on this connection (see #11 설계 문서, "요청 동시성" 절).
MAX_IN_FLIGHT = 8

SendFn = Callable[[Dict[str, Any]], None]


class SyncWsSession:
    def __init__(self, repository, send: SendFn):
        self.repository = repository
        self._send = send
        self.authenticated = False
        self.owner_username: Optional[str] = None
        self.vault_id: Optional[str] = None
        self.device_name: str = ""
        self.user_name: str = ""

    def handle_text_message(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            self._send_error(0, "INVALID_JSON", "Malformed JSON message")
            return

        request_id = envelope.get("requestId", 0)
        op = envelope.get("op")
        payload = envelope.get("payload") or {}

        if not self.authenticated:
            if op != "init":
                self._send_error(request_id, "UNAUTHENTICATED", "Must send init first")
                return
            self._handle_init(request_id, payload)
            return

        if op == "ping":
            self._send_pong(request_id)
        else:
            self._send_error(request_id, "UNKNOWN_OP", f"Unknown op: {op}")

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

        self._send({
            "requestId": request_id,
            "op": "init_ok",
            "payload": {
                "serverVersion": "0.1.0",
                "timestampMs": int(time.time() * 1000),
                "maxInFlight": MAX_IN_FLIGHT,
            },
        })

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
    def __init__(self, repository, reactor=None):
        super().__init__()
        self._repository = repository
        self._reactor = reactor if reactor is not None else _default_reactor
        self._session: Optional[SyncWsSession] = None
        self._init_timeout_call = None

    def onOpen(self):
        self._session = SyncWsSession(self._repository, send=self._send_json)
        self._init_timeout_call = self._reactor.callLater(INIT_TIMEOUT_SECONDS, self._on_init_timeout)

    def onMessage(self, payload, isBinary):
        if isBinary:
            # File-chunk binary frames are handled once upload/download ops land (PR2).
            return

        assert self._session is not None
        self._session.handle_text_message(payload.decode("utf-8"))

        if self._session.authenticated and self._init_timeout_call and self._init_timeout_call.active():
            self._init_timeout_call.cancel()

    def onClose(self, wasClean, code, reason):
        if self._init_timeout_call and self._init_timeout_call.active():
            self._init_timeout_call.cancel()

    def _on_init_timeout(self):
        if not self._session or not self._session.authenticated:
            logger.info("WS connection dropped: no init within %.0fs", INIT_TIMEOUT_SECONDS)
            self.sendClose(code=4001, reason="init timeout")

    def _send_json(self, message: Dict[str, Any]) -> None:
        self.sendMessage(json.dumps(message).encode("utf-8"), isBinary=False)


def make_ws_sync_resource(repository) -> WebSocketResource:
    factory = WebSocketServerFactory()
    factory.protocol = lambda: SyncWebSocketProtocol(repository)
    return WebSocketResource(factory)
