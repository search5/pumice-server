"""GET /watch?vault_id=<id> -- a plain (non-gRPC) Server-Sent Events endpoint that tells an
already-connected client "something changed, go run Delta" so it doesn't have to wait for its
next poll/interval. Deliberately outside sync.proto/gRPC-Web entirely (no protobuf framing to
gain from using it here) -- dispatched by RootResource exactly like the Pyramid REST resource
already is, just another plain-HTTP sibling. See #10_실시간_변경_알림_구현_계획.md.
"""

import logging
import os

from twisted.internet import reactor as _default_reactor
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET, Request

from .change_notifier import ChangeNotifier
from .streaming_upload_resource import resolve_owner_via_thread

logger = logging.getLogger(__name__)

# Traefik always sits in front of the real deployment (see #10 계획서 3-1절) and its exact idle
# timeout isn't visible from this repo -- kept conservative and overridable without a code change.
HEARTBEAT_SECONDS = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15"))


def watch_cors_options_headers(origin: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"Access-Control-Allow-Origin", origin),
        (b"Access-Control-Allow-Methods", b"GET, OPTIONS"),
        (b"Access-Control-Allow-Headers", b"authorization"),
        (b"Access-Control-Allow-Credentials", b"true"),
    ]


class WatchResource(Resource):
    isLeaf = True

    def __init__(self, repository, notifier: ChangeNotifier, reactor=None):
        super().__init__()
        self._repository = repository
        self._notifier = notifier
        self._reactor = reactor if reactor is not None else _default_reactor
        self._resolve_owner = resolve_owner_via_thread(repository)

    def render_GET(self, request: Request) -> bytes:
        vault_id_list = request.args.get(b"vault_id", [])
        if not vault_id_list:
            request.setResponseCode(400)
            return b"Missing vault_id"
        vault_id = vault_id_list[0].decode("utf-8", errors="replace")

        def on_authorized(owner: str) -> None:
            if request._disconnected:  # client already gone while auth was resolving
                return
            self._start_stream(request, owner, vault_id)

        def on_auth_failed(failure) -> None:
            logger.info(f"WatchResource auth failed: {failure.getErrorMessage()}")
            if not request.finished:
                request.setResponseCode(401)
                request.write(b"Unauthorized")
                request.finish()

        self._resolve_owner(request).addCallbacks(on_authorized, on_auth_failed)
        return NOT_DONE_YET

    def render_OPTIONS(self, request: Request) -> bytes:
        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(204)
        for name, value in watch_cors_options_headers(origin):
            request.setHeader(name, value)
        request.setHeader(b"Content-Length", b"0")
        return b""

    def _start_stream(self, request: Request, owner: str, vault_id: str) -> None:
        # This connection's whole point is to sit mostly-silent from the client's side for a
        # long time -- Twisted's Site-level idle timeout (main.py's Site(timeout=60)) only
        # resets on bytes *received* from the client (HTTPChannel.dataReceived), never on bytes
        # *we* write, so our own heartbeat frames below do not keep that timeout from firing.
        # Disabling it for this one connection is the only way; no need to restore it
        # afterward since the connection ends when the stream does.
        channel = getattr(request, "channel", None)
        if channel is not None and hasattr(channel, "setTimeout"):
            channel.setTimeout(None)

        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"text/event-stream")
        request.setHeader(b"Cache-Control", b"no-cache")
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")
        # Sent immediately, before the first real event -- Traefik (or any intermediary) sees a
        # complete response head right away instead of waiting on one that never seems to arrive.
        request.write(b": connected\n\n")

        def on_change() -> None:
            if not request.finished:
                request.write(b"data: changed\n\n")

        unsubscribe = self._notifier.subscribe(owner, vault_id, on_change)

        heartbeat_call = [None]  # boxed so the nested functions below can rebind it

        def send_heartbeat() -> None:
            if request.finished:
                return
            request.write(b": keepalive\n\n")
            heartbeat_call[0] = self._reactor.callLater(HEARTBEAT_SECONDS, send_heartbeat)

        heartbeat_call[0] = self._reactor.callLater(HEARTBEAT_SECONDS, send_heartbeat)

        def cleanup(_ignored=None) -> None:
            unsubscribe()
            if heartbeat_call[0] is not None and heartbeat_call[0].active():
                heartbeat_call[0].cancel()

        request.notifyFinish().addBoth(cleanup)
