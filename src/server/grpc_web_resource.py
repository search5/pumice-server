import asyncio
import base64
import inspect
import logging
import struct
from urllib.parse import quote

import grpc
from twisted.web import server
from twisted.web.resource import Resource

from . import sync_pb2

logger = logging.getLogger(__name__)

# ─── gRPC-Web wire framing ──────────────────────────────────────────────────
# Each message on the wire is: 1 flag byte (bit 0x80 = this is the trailer
# frame) + 4-byte big-endian length + payload. In "grpc-web-text" mode the
# whole frame is additionally base64-encoded. See sonora's protocol.py / the
# gRPC-Web spec for the reference implementation this mirrors.

_HEADER_FORMAT = ">BI"
_HEADER_LENGTH = struct.calcsize(_HEADER_FORMAT)
_TRAILERS_FLAG = 0x80


def wrap_message(trailers: bool, payload: bytes) -> bytes:
    flags = _TRAILERS_FLAG if trailers else 0
    return struct.pack(_HEADER_FORMAT, flags, len(payload)) + payload


def unwrap_message(data: bytes):
    if len(data) < _HEADER_LENGTH:
        raise ValueError("Truncated gRPC-Web message header")
    flags, length = struct.unpack(_HEADER_FORMAT, data[:_HEADER_LENGTH])
    payload = data[_HEADER_LENGTH : _HEADER_LENGTH + length]
    if len(payload) != length:
        raise ValueError("Truncated gRPC-Web message body")
    return bool(flags & _TRAILERS_FLAG), payload


def pack_trailers(trailers) -> bytes:
    return "".join(f"{k.lower()}: {v}\r\n" for k, v in trailers).encode("ascii")


# method name -> (request class, streaming response)
_METHODS = {
    "Ping": (sync_pb2.Empty, False),
    "Delta": (sync_pb2.DeltaRequest, False),
    "UploadFiles": (sync_pb2.UploadBatch, True),
    "DownloadFiles": (sync_pb2.DownloadBatchRequest, True),
    "GetFileHistory": (sync_pb2.HistoryRequest, False),
    "DownloadHistoryVersion": (sync_pb2.HistoryVersionDownloadRequest, True),
    "RestoreHistoryVersion": (sync_pb2.RestoreHistoryRequest, False),
}

_CORS_ALLOW_HEADERS = (
    b"authorization, content-type, x-grpc-web, x-user-agent, grpc-timeout, "
    b"obs-token, obs-id, obs-path, obs-hash, x-device-name, x-user-name"
)


class Aborted(Exception):
    """Raised by NativeServicerContext.abort() to unwind out of a servicer method
    while carrying the status code/details that should go into the trailers."""

    def __init__(self, code, details):
        super().__init__(details)
        self.code = code
        self.details = details


class NativeServicerContext:
    """Minimal grpc.ServicerContext-compatible shim over a Twisted Request.

    Only implements what SyncServiceServicer (server/service.py) actually calls:
    invocation_metadata(), abort(), and write(). abort() is deliberately a plain
    (non-async) method that raises immediately -- service.py calls it both as
    `context.abort(...)` and `await context.abort(...)`, and since the call raises
    before returning anything, both forms behave identically (the `await` never
    gets anything to wait on).
    """

    def __init__(self, request, wrap_response_message):
        self._request = request
        self._wrap = wrap_response_message
        self.code = grpc.StatusCode.OK
        self.details = None

    def invocation_metadata(self):
        result = []
        for name, values in self._request.requestHeaders.getAllRawHeaders():
            header = name.decode("ascii").lower()
            for value in values:
                result.append((header, value.decode("utf-8")))
        return result

    def abort(self, code, details):
        self.code = code
        self.details = details
        raise Aborted(code, details)

    async def write(self, message):
        self._request.write(self._wrap(False, message.SerializeToString()))


class SyncServiceResource(Resource):
    """Serves the SyncService gRPC-Web methods directly on top of Twisted, with
    no WSGI/sonora/thread-pool bridge in between -- SyncServiceServicer's async
    methods are driven straight off the reactor's event loop via
    Deferred.fromCoroutine."""

    isLeaf = True
    _PATH_PREFIX = "/obsidian.sync.v1.SyncService/"

    def __init__(self, servicer):
        super().__init__()
        self.servicer = servicer

    def render(self, request):
        if request.method == b"OPTIONS":
            return self._render_preflight(request)

        if request.method != b"POST":
            request.setResponseCode(400)
            return b""

        path = request.path.decode("utf-8")
        if not path.startswith(self._PATH_PREFIX):
            request.setResponseCode(404)
            return b""

        method_name = path[len(self._PATH_PREFIX) :]
        spec = _METHODS.get(method_name)
        if spec is None:
            request.setResponseCode(404)
            return b""

        # Driven as a plain asyncio Task (not Deferred.fromCoroutine) because
        # SyncServiceServicer relies on asyncio.to_thread internally, and Twisted's
        # coroutine-driving doesn't bridge raw asyncio Futures (the kind
        # loop.run_in_executor returns) even under asyncioreactor -- it only understands
        # Deferreds. A plain Task, scheduled on the same reactor-driven loop, handles it
        # exactly the way asyncio itself would.
        task = asyncio.ensure_future(self._handle(request, method_name, spec))
        task.add_done_callback(lambda t: self._on_task_done(t, request))
        return server.NOT_DONE_YET

    def _render_preflight(self, request):
        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(204)
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Methods", b"POST, GET, OPTIONS, PUT, DELETE")
        request.setHeader(b"Access-Control-Allow-Headers", _CORS_ALLOW_HEADERS)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")
        request.setHeader(b"Access-Control-Expose-Headers", b"grpc-status, grpc-message")
        request.setHeader(b"Content-Length", b"0")
        request.setHeader(b"Content-Type", b"text/plain")
        return b""

    def _on_task_done(self, task, request):
        exc = task.exception() if not task.cancelled() else None
        if exc is None:
            return
        logger.error("Unhandled error serving gRPC-Web request", exc_info=exc)
        try:
            if not request.finished:
                request.setResponseCode(500)
                request.finish()
        except Exception:
            pass

    async def _handle(self, request, method_name, spec):
        request_cls, streaming = spec

        request_content_type = (request.getHeader(b"content-type") or b"").decode("ascii")
        is_text_request = request_content_type.startswith("application/grpc-web-text")

        accept = (
            (request.getHeader(b"accept") or b"application/grpc-web+proto")
            .decode("ascii")
            .split(",")[0]
            .strip()
        )
        is_text_response = accept.startswith("application/grpc-web-text")

        def wrap_response_message(trailers, payload):
            data = wrap_message(trailers, payload)
            if is_text_response:
                data = base64.b64encode(data)
            return data

        body = request.content.read()
        if is_text_request:
            body = base64.b64decode(body)

        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", accept.encode("ascii"))
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")
        request.setHeader(b"Access-Control-Expose-Headers", b"grpc-status, grpc-message")

        context = NativeServicerContext(request, wrap_response_message)
        message_bytes = None

        try:
            _, payload = unwrap_message(body)
            req_message = request_cls()
            req_message.ParseFromString(payload)

            handler = getattr(self.servicer, method_name)
            result = handler(req_message, context)

            if streaming:
                if inspect.isasyncgen(result):
                    async for message in result:
                        await context.write(message)
                else:
                    await result
            else:
                response = await result
                if response is not None:
                    message_bytes = wrap_response_message(False, response.SerializeToString())
        except Aborted:
            pass
        except Exception as e:
            logger.error(f"Unhandled error in {method_name}: {e}", exc_info=True)
            context.code = grpc.StatusCode.INTERNAL
            context.details = str(e)

        if message_bytes is not None:
            request.write(message_bytes)

        trailers = [("grpc-status", str(context.code.value[0]))]
        if context.details:
            trailers.append(("grpc-message", quote(context.details)))

        request.write(wrap_response_message(True, pack_trailers(trailers)))
        request.finish()


class RootResource(Resource):
    """Routes SyncService gRPC-Web calls to a native SyncServiceResource and
    everything else (the Pyramid web UI) to a WSGIResource, on the same port.
    gRPC-Web paths contain dots (e.g. /obsidian.sync.v1.SyncService/Delta), so
    this dispatches on the full decoded path rather than per-segment child
    lookup."""

    isLeaf = True

    def __init__(self, sync_resource, fallback_resource):
        super().__init__()
        self._sync_resource = sync_resource
        self._fallback_resource = fallback_resource

    def render(self, request):
        if request.path.decode("utf-8").startswith(SyncServiceResource._PATH_PREFIX):
            return self._sync_resource.render(request)
        return self._fallback_resource.render(request)
