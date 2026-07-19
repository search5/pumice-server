"""A twisted.web.server.Request subclass that streams a designated upload path's body
directly into an EnvelopeStreamParser as bytes arrive, instead of Twisted's default
full-buffering-into-self.content -- with three production concerns layered on top of the
PoC design verified at /home/jiho/twisted-streaming-poc (28 tests):

1. Auth-before-streaming: the caller's device token is resolved from the Authorization
   header *before* a single body byte is handed to the parser. An invalid/missing token
   rejects with 401 and never processes any of the (possibly attacker-controlled) body.
2. Backpressure: resolving the token, and every subsequent frame's disk I/O, is treated as
   work the caller must wait for -- the transport is paused (stops reading more socket
   bytes) the moment there is any such work outstanding for this request, and only resumed
   once it drains. This bounds how far ahead of actual disk throughput a fast/malicious
   sender can push data, instead of letting it all queue up in memory.
3. Blocking I/O avoidance: token resolution and all file I/O (open/write/close, the
   existing SyncServiceServicer._finalize_uploaded_file on EOF) run via
   twisted.internet.threads.deferToThread rather than inline, so none of it blocks the
   reactor thread -- matching the asyncio.to_thread convention already used throughout
   service.py for the exact same operations on the non-streaming upload path.

Hook-point rationale (verified against Twisted 26.4.0 source, matching this project's pinned
version): HTTPChannel.headerReceived() appends every header onto req.requestHeaders as each
header line is parsed, and allHeadersReceived() (which calls req.gotLength()) only runs once
all headers are in -- so by the time gotLength() runs, the Authorization header is already
available via self.requestHeaders, even though Request.path/.method (set in requestReceived(),
which only fires after the full body arrives) are not. Path is available early only via the
channel's private `self.channel._path` (same trick the PoC uses) -- there is no public API for
it this early either.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict
from urllib.parse import unquote

from twisted.internet import defer, threads
from twisted.python.failure import Failure
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET, Request

from . import sync_pb2
from .grpc_web_resource import UPLOAD_STREAM_PATH, wrap_message
from .streaming import EnvelopeStreamParser, FrameTooLargeError, OnFrame

logger = logging.getLogger(__name__)

__all__ = [
    "UPLOAD_STREAM_PATH",
    "StreamingAuthError",
    "StreamingUploadMetrics",
    "StreamingUploadRequest",
    "StreamingUploadResource",
    "make_streaming_upload_request_factory",
    "metrics",
    "resolve_owner_username",
    "resolve_owner_via_thread",
]


@dataclass
class StreamingUploadMetrics:
    """In-process counters for the streaming upload path -- there's no existing metrics
    stack in pumice-server (no prometheus_client or similar dependency) to plug into, so
    this is a deliberately lightweight, dependency-free alternative: exposed via the admin
    API (web.py's admin_streaming_stats_view) and a periodic log summary (main.py). Not
    persisted -- resets on restart, which is fine for an "is something wrong right now"
    signal, not a billing/audit log.

    All mutations happen on the reactor thread only (from StreamingUploadRequest /
    _UploadAccumulator callbacks -- never from inside a blocking worker-thread handler), so
    no locking is needed. The module-level `metrics` singleton is what production wiring
    uses; tests inject a fresh instance instead of touching shared global state."""

    active_uploads: int = 0
    total_uploads_started: int = 0
    total_files_succeeded: int = 0
    total_files_failed: int = 0
    total_bytes_received: int = 0
    rejections: Dict[str, int] = field(default_factory=lambda: {
        "auth_failed": 0,
        "auth_timeout": 0,
        "frame_too_large": 0,
        "malformed_frame": 0,
        "max_duration_exceeded": 0,
    })
    file_failure_reasons: Dict[str, int] = field(default_factory=lambda: {
        "invalid_vault": 0,
        "invalid_path": 0,
        "temp_file_error": 0,
        "path_mismatch": 0,
        "hash_mismatch": 0,
        "other": 0,
    })
    backpressure_pause_events: int = 0
    backpressure_paused_seconds_total: float = 0.0

    def upload_started(self) -> None:
        self.active_uploads += 1
        self.total_uploads_started += 1

    def upload_finished(self) -> None:
        self.active_uploads -= 1

    def record_rejection(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def record_file_result(self, ok: bool, reason: str | None) -> None:
        if ok:
            self.total_files_succeeded += 1
        else:
            self.total_files_failed += 1
            key = reason if reason in self.file_failure_reasons else "other"
            self.file_failure_reasons[key] += 1

    def record_bytes_received(self, count: int) -> None:
        self.total_bytes_received += count

    def record_backpressure_pause(self, duration_seconds: float) -> None:
        self.backpressure_pause_events += 1
        self.backpressure_paused_seconds_total += duration_seconds

    def snapshot(self) -> dict:
        return {
            "active_uploads": self.active_uploads,
            "total_uploads_started": self.total_uploads_started,
            "total_files_succeeded": self.total_files_succeeded,
            "total_files_failed": self.total_files_failed,
            "total_bytes_received": self.total_bytes_received,
            "rejections": dict(self.rejections),
            "file_failure_reasons": dict(self.file_failure_reasons),
            "backpressure_pause_events": self.backpressure_pause_events,
            "backpressure_paused_seconds_total": round(self.backpressure_paused_seconds_total, 3),
        }


metrics = StreamingUploadMetrics()


class StreamingAuthError(Exception):
    """Raised (as a Deferred failure, not directly) when the Authorization header is
    missing or doesn't resolve to a known device token. Mirrors the token-resolution half
    of SyncServiceServicer._verify_vault_access, but runs earlier (before any body byte is
    read) and doesn't need a full ServicerContext."""


def resolve_owner_username(request: Request, repository) -> str:
    """Pure, synchronous lookup -- callers needing this off the reactor thread should go
    through resolve_owner_via_thread() instead of calling this directly."""
    raw = request.requestHeaders.getRawHeaders(b"authorization")
    if not raw:
        raise StreamingAuthError("Missing authorization header")
    auth_header = raw[0].decode("utf-8", errors="replace")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header.strip()
    if not token:
        raise StreamingAuthError("Missing authorization header")

    device = repository.get_device_token(token)
    if not device:
        raise StreamingAuthError("Invalid or missing auth token")
    return device["username"]


def resolve_owner_via_thread(repository):
    """Returns a ResolveOwner callable (Request -> Deferred[str]) suitable for production
    use with StreamingUploadRequest -- the actual DB lookup runs on the reactor's thread
    pool via deferToThread, never inline on the reactor thread."""

    def resolve(request: Request) -> defer.Deferred:
        return threads.deferToThread(resolve_owner_username, request, repository)

    return resolve


ShouldStream = Callable[[bytes], bool]
ResolveOwner = Callable[[Request], "defer.Deferred[str]"]

# A DB lookup should be near-instant; this only needs to be generous enough to not trip on
# ordinary load, not to accommodate a legitimately slow operation. If it fires, the request
# and its paused transport stop waiting -- but note this does NOT free up the underlying
# worker thread: deferToThread can't interrupt a thread that's actually still blocked in the
# DB driver, it only stops *this Deferred* from waiting on it further (see _onAuthFailed).
AUTH_TIMEOUT_SECONDS = 10.0

# A ceiling on total time-to-fully-received-body, independent of activity -- unlike
# Twisted's own idle timeout (TimeoutMixin, reset on every byte received, off by default --
# see main.py), this catches a slow-loris-style sender that drips bytes just often enough to
# never go idle but never actually finishes. 10 minutes is generous enough for a large,
# legitimately slow initial-vault-sync upload over a poor connection while still bounding
# how long a single stalled/malicious connection can occupy a server slot.
MAX_UPLOAD_SECONDS = 600.0


class StreamingUploadRequest(Request):
    """Set channel.requestFactory to a factory built by
    make_streaming_upload_request_factory() rather than instantiating this directly -- it
    needs should_stream/resolve_owner/on_frame/clock injected per-request before gotLength()
    runs.
    """

    _should_stream: ShouldStream
    _resolve_owner: ResolveOwner
    _on_frame: OnFrame
    _clock: object  # IReactorTime -- the real reactor in production, task.Clock() in tests
    _metrics: StreamingUploadMetrics
    _stream_parser: EnvelopeStreamParser | None
    _stream_aborted: bool = False
    _awaiting_auth: bool = False
    _max_duration_call: object | None = None  # IDelayedCall | None
    owner_username: str | None = None

    def gotLength(self, length: int | None) -> None:
        super().gotLength(length)
        self._pending_bytes = bytearray()
        self._pending_request_received_args = None

        # CORS preflight (OPTIONS) requests to this same path must never be treated as
        # auth-gated streaming bodies: browsers deliberately omit Authorization on a preflight
        # (CORS spec), so resolve_owner() would always fail it with 401 -- and _abort() writes
        # a bare status line with no CORS headers at all, which breaks the preflight itself
        # (confirmed via a real browser: it fails with "No 'Access-Control-Allow-Origin'
        # header is present" instead of ever reaching render_OPTIONS()). self.channel._command
        # holds the HTTP method just as early/privately as _path (both parsed together from
        # the request line in HTTPChannel.lineReceived()) -- see module docstring for why only
        # private attributes are available this early.
        path = getattr(self.channel, "_path", None)
        method = getattr(self.channel, "_command", None)
        if method != b"OPTIONS" and path is not None and self._should_stream(path):
            self._stream_parser = None
            self._awaiting_auth = True
            self._metrics.upload_started()
            logger.info("streaming upload started")
            # notifyFinish() fires exactly once regardless of how the request ends --
            # normal finish() or an abrupt loseConnection()/connection loss -- so this is
            # the single reliable place to balance the upload_started() above (rather than
            # duplicating a decrement into every abort path plus the success path).
            self.notifyFinish().addBoth(lambda _: self._metrics.upload_finished())
            # Ceiling on the whole request's time-to-fully-received, cancelled once that
            # happens (see requestReceived()) or the request is aborted for any other reason
            # (see _abort()) -- never left dangling either way.
            self._max_duration_call = self._clock.callLater(
                MAX_UPLOAD_SECONDS, self._onMaxDurationExceeded
            )
            # Stop reading more body bytes off the socket until we know whether this
            # caller is even allowed to send them -- see module docstring point 2.
            self.transport.pauseProducing()
            deferred = self._resolve_owner(self)
            # Must come before addCallbacks(): addTimeout() only wraps callbacks/errbacks
            # already attached at the point it's called, so this ensures a timeout is
            # converted to a TimeoutError *before* _onAuthFailed sees it (see _onAuthFailed's
            # failure.check(defer.TimeoutError) branch).
            deferred.addTimeout(AUTH_TIMEOUT_SECONDS, self._clock)
            deferred.addCallbacks(self._onAuthResolved, self._onAuthFailed)
        else:
            self._stream_parser = None

    def handleContentChunk(self, data: bytes) -> None:
        if self._stream_aborted:
            # Connection is already being torn down -- see _onAuthFailed / the except
            # clause below. Drop further bytes rather than reprocessing them.
            return
        if self._awaiting_auth:
            # Bytes that arrived in the same read as the headers, before pauseProducing()
            # had a chance to take effect -- buffer them and replay once auth resolves
            # (see _onAuthResolved), rather than dropping or processing them prematurely.
            self._pending_bytes.extend(data)
            return
        if self._stream_parser is not None:
            try:
                self._stream_parser.feed(data)
            except Exception as exc:
                # See streaming_poc's StreamingRequest (the PoC this was ported from) for
                # why this needs to be caught explicitly: Twisted's own
                # _IdentityTransferDecoder.dataCallback() call site does not catch generic
                # exceptions, so an uncaught FrameTooLargeError or on_frame bug here would
                # otherwise propagate into HTTPChannel.dataReceived() itself.
                logger.warning(
                    "streaming upload body processing failed; aborting connection",
                    exc_info=True,
                )
                reason = "frame_too_large" if isinstance(exc, FrameTooLargeError) else "malformed_frame"
                self._abort(b"400 Bad Request", reason)
        else:
            super().handleContentChunk(data)

    def requestReceived(self, command: bytes, path: bytes, version: bytes) -> None:
        if self._stream_aborted:
            # HTTPChannel.allContentReceived() calls this unconditionally once
            # Content-Length bytes are exhausted, even for a request we already responded
            # to and disconnected above -- skip process()/render()/finish() entirely (see
            # the PoC's identical override for the full explanation, including the
            # StringTransport "resume producing after loseConnection" crash this avoids).
            return
        if self._awaiting_auth:
            # A request with a body small/fast enough to fully arrive before the auth
            # Deferred resolves would otherwise reach process()/render() while
            # owner_username is still unknown -- pauseProducing() in gotLength() only
            # stops *future* socket reads, it doesn't delay bytes already delivered within
            # the current dataReceived() call from reaching allContentReceived() and
            # firing this synchronously. Defer until auth actually resolves (see
            # _onAuthResolved/_onAuthFailed) instead of racing it.
            self._pending_request_received_args = (command, path, version)
            return
        self._cancelMaxDurationTimer()
        super().requestReceived(command, path, version)

    def _cancelMaxDurationTimer(self) -> None:
        if self._max_duration_call is not None and self._max_duration_call.active():
            self._max_duration_call.cancel()
        self._max_duration_call = None

    def _onMaxDurationExceeded(self) -> None:
        logger.warning("streaming upload exceeded max duration; aborting connection")
        self._max_duration_call = None  # already fired -- nothing left to cancel
        self._awaiting_auth = False
        self._pending_request_received_args = None
        self._abort(b"408 Request Timeout", "max_duration_exceeded")

    def _onAuthResolved(self, owner_username: str) -> None:
        self.owner_username = owner_username
        self._awaiting_auth = False
        self._stream_parser = EnvelopeStreamParser(
            on_frame=lambda flags, payload: self._on_frame(self, flags, payload)
        )
        self.transport.resumeProducing()

        pending = bytes(self._pending_bytes)
        self._pending_bytes = bytearray()
        if pending:
            self.handleContentChunk(pending)

        if self._pending_request_received_args is not None:
            args = self._pending_request_received_args
            self._pending_request_received_args = None
            self.requestReceived(*args)

    def _onAuthFailed(self, failure) -> None:
        self._awaiting_auth = False
        self._pending_request_received_args = None  # connection is being aborted; discard
        if failure.check(defer.TimeoutError):
            logger.warning("streaming upload auth resolution timed out")
            self._abort(b"503 Service Unavailable", "auth_timeout")
        else:
            logger.warning("streaming upload rejected: %s", failure.getErrorMessage())
            self._abort(b"401 Unauthorized", "auth_failed")

    def _abort(self, status_line: bytes, reason: str) -> None:
        self._stream_aborted = True
        self._cancelMaxDurationTimer()  # every abort path must not leave this dangling
        self._metrics.record_rejection(reason)
        self.transport.write(b"HTTP/1.1 " + status_line + b"\r\n\r\n")
        self.loseConnection()


def make_streaming_upload_request_factory(
    should_stream: ShouldStream,
    resolve_owner: ResolveOwner,
    on_frame: OnFrame,
    clock=None,
    upload_metrics: StreamingUploadMetrics | None = None,
):
    """Returns a channel.requestFactory-compatible callable. Matches the legacy
    (channel, queued) signature HTTPChannel.lineReceived() falls back to when the factory
    doesn't provide INonQueuedRequestFactory.

    clock defaults to the real reactor (production use) -- pass a twisted.internet.task.Clock
    for deterministic timeout tests. upload_metrics defaults to the shared module-level
    `metrics` singleton -- pass a fresh StreamingUploadMetrics() in tests to avoid touching
    shared global state."""
    if clock is None:
        from twisted.internet import reactor as clock
    if upload_metrics is None:
        upload_metrics = metrics

    def factory(channel, queued=0):
        request = StreamingUploadRequest(channel, queued)
        request._should_stream = should_stream
        request._resolve_owner = resolve_owner
        request._on_frame = on_frame
        request._clock = clock
        request._metrics = upload_metrics
        return request

    return factory


# ─── Per-request file accumulation ─────────────────────────────────────────────────────
#
# Each streamed request can carry many files, each as [header, data..., eof] FileChunk
# frames (same shape as UploadFiles' UploadBatch.chunks, just delivered as separate wire
# frames instead of bundled into one message). _UploadAccumulator holds the per-request
# state (current file handle, hash, per-vault metadata caches) and serializes all blocking
# work for the request through a single Deferred chain -- see _enqueue() -- so that e.g.
# two "data" frames for the same file can never race each other's disk writes, no matter
# how many complete frames EnvelopeStreamParser.feed() hands to on_frame synchronously in
# one call (it does not yield between frames already fully buffered).


@dataclass
class _FrameResult:
    """Return type of every _handle_*_blocking() method -- ack is the (path, ok, error) to
    eventually flush as an UploadAck (None for frame types that don't ack, like header/data
    on success), reason is a short slug for metrics.record_file_result() bucketing on
    failure (ignored on success), bytes_received lets _after() -- back on the reactor thread
    -- update metrics.total_bytes_received without the worker thread touching metrics
    directly (see StreamingUploadMetrics's docstring on why mutation is reactor-thread-only).
    """

    ack: tuple[str, bool, str] | None = None
    reason: str | None = None
    bytes_received: int = 0


class _UploadAccumulator:
    def __init__(self, request: Request, servicer, upload_metrics: StreamingUploadMetrics | None = None):
        self.request = request
        self.servicer = servicer
        self.metrics = upload_metrics if upload_metrics is not None else metrics
        self.file_handle = None
        self.current_rel_path = None
        self.current_temp_path = None
        self.current_file_path = None
        self.total_bytes = 0
        self.modified_at_ms = 0
        self.sha256_hash = None
        self.vault_id = None
        self.metadata_caches: Dict[str, dict] = {}
        self.tombstone_indexes: Dict[str, dict] = {}

        # Acks are collected here rather than written via request.write() as each file
        # finishes -- this connection is plain HTTP/1.1 (the app server never terminates
        # TLS/HTTP2 itself; see main.py/README), and under HTTP/1.1 a response cannot start
        # until the *entire* request has been received, no matter how early Twisted's
        # Request-level API would technically let write() be called. Confirmed by actually
        # running this against a real client: writing early corrupts the response (Twisted
        # falls back to its "(no clientproto yet)" placeholder status line because
        # requestReceived() -- which sets clientproto -- hasn't run yet). StreamingUploadResource
        # flushes pending_acks in one batch once render_POST() actually starts the response,
        # exactly matching the existing (non-streaming) UploadFiles RPC's ack-after-full-body
        # behaviour -- the streaming win here is incremental *request* processing (no full
        # in-memory buffering, real backpressure against slow disk), not an interleaved response.
        self.pending_acks: list[tuple[str, bool, str]] = []

        self._chain: defer.Deferred = defer.succeed(None)
        self._inflight = 0

    def handle_frame(self, flags: int, payload: bytes) -> None:
        chunk = sync_pb2.FileChunk()
        chunk.ParseFromString(payload)
        payload_type = chunk.WhichOneof("payload")
        if payload_type == "header":
            self._enqueue(self._handle_header_blocking, chunk.header)
        elif payload_type == "data":
            self._enqueue(self._handle_data_blocking, chunk.data)
        elif payload_type == "eof":
            self._enqueue(self._handle_eof_blocking, chunk.eof)

    def abort(self) -> None:
        # Called if the request itself gets aborted (e.g. FrameTooLargeError) with a file
        # still open -- clean up on the same thread-serialized chain so it can't race a
        # write that's still in flight.
        self._enqueue(self._abort_blocking, None)

    def _enqueue(self, blocking_fn, arg) -> None:
        if self._inflight == 0:
            # First piece of outstanding work for this request -- stop reading more body
            # bytes until the backlog drains back to zero (see module docstring point 2).
            self.request.transport.pauseProducing()
            self._pause_started_at = time.monotonic()
        self._inflight += 1

        def _run(_ignored):
            return threads.deferToThread(blocking_fn, arg)

        def _after(result_or_failure) -> None:
            # addBoth, not addCallback+addErrback: the inflight/pauseProducing bookkeeping
            # below must run whether blocking_fn returned normally or raised (e.g. an
            # unhandled OSError writing to disk) -- an earlier version used addCallback for
            # this part and addErrback separately for logging, which meant an unexpected
            # exception skipped the bookkeeping entirely and left the transport paused
            # forever (a real deadlock, not just a missed metric).
            self._inflight -= 1
            if self._inflight == 0:
                self.request.transport.resumeProducing()
                self.metrics.record_backpressure_pause(time.monotonic() - self._pause_started_at)

            if isinstance(result_or_failure, Failure):
                logger.error(
                    "streaming upload frame handling failed",
                    exc_info=(
                        result_or_failure.type,
                        result_or_failure.value,
                        result_or_failure.getTracebackObject(),
                    ),
                )
                self.metrics.record_file_result(False, "other")
                return None

            result: _FrameResult = result_or_failure
            if result.bytes_received:
                self.metrics.record_bytes_received(result.bytes_received)
            if result.ack is not None:
                path, ok, error = result.ack
                self.pending_acks.append((path, ok, error))
                self.metrics.record_file_result(ok, result.reason)
            return None

        self._chain = self._chain.addCallback(_run).addBoth(_after)

    # --- blocking (worker-thread) handlers -------------------------------------------------
    # Every method below runs on reactor.getThreadPool(), never on the reactor thread itself.

    def _handle_header_blocking(self, header):
        if self.file_handle:
            # A previous file was left unfinished (e.g. client moved on without sending
            # EOF) -- clean it up before starting the next one.
            self._close_and_discard_current()

        vault_id = header.vault_id
        try:
            vault_path = self.servicer._get_vault_path(self.request.owner_username, vault_id)
        except ValueError:
            return _FrameResult(ack=(header.path, False, "Invalid vault ID"), reason="invalid_vault")

        rel_path = os.path.normpath(header.path).replace("\\", "/")
        if rel_path.startswith("..") or os.path.isabs(rel_path):
            return _FrameResult(ack=(header.path, False, "Invalid file path"), reason="invalid_path")

        if vault_id not in self.metadata_caches:
            files = self.servicer.repository.load_all(self.request.owner_username, vault_id)
            self.metadata_caches[vault_id] = files
            self.tombstone_indexes[vault_id] = {
                meta["content_hash"]: path
                for path, meta in files.items()
                if meta.get("is_deleted") and meta.get("content_hash")
            }

        temp_dir = os.path.join(self.servicer.data_dir, "tmp", self.request.owner_username, vault_id)
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"{secrets.token_hex(8)}.tmp")

        try:
            self.file_handle = open(temp_path, "wb")
        except Exception as e:
            return _FrameResult(
                ack=(header.path, False, f"Failed to create temp file: {e}"),
                reason="temp_file_error",
            )

        self.vault_id = vault_id
        self.current_rel_path = rel_path
        self.current_temp_path = temp_path
        self.current_file_path = os.path.join(vault_path, rel_path)
        self.total_bytes = header.total_bytes
        self.modified_at_ms = header.modified_at_ms
        self.sha256_hash = hashlib.sha256()
        return _FrameResult()  # no ack for header frames -- matches UploadFiles' existing behaviour

    def _handle_data_blocking(self, data_payload):
        if not self.file_handle:
            logger.warning("streaming upload received data chunk but no file is open")
            return _FrameResult()

        chunk_path = os.path.normpath(data_payload.path).replace("\\", "/")
        if chunk_path != self.current_rel_path:
            self._close_and_discard_current()
            return _FrameResult(
                ack=(chunk_path, False, "Path mismatch in data chunk"), reason="path_mismatch"
            )

        self.file_handle.write(data_payload.data)
        self.sha256_hash.update(data_payload.data)
        return _FrameResult(bytes_received=len(data_payload.data))

    def _handle_eof_blocking(self, eof_payload):
        if not self.file_handle:
            logger.warning("streaming upload received eof chunk but no file is open")
            return _FrameResult()

        chunk_path = os.path.normpath(eof_payload.path).replace("\\", "/")
        if chunk_path != self.current_rel_path:
            self._close_and_discard_current()
            return _FrameResult(
                ack=(chunk_path, False, "Path mismatch in eof chunk"), reason="path_mismatch"
            )

        self.file_handle.close()
        self.file_handle = None

        calculated_hash = self.sha256_hash.hexdigest()
        if calculated_hash != eof_payload.content_hash:
            if os.path.exists(self.current_temp_path):
                os.remove(self.current_temp_path)
            return _FrameResult(
                ack=(eof_payload.path, False, "Hash verification failed"), reason="hash_mismatch"
            )

        device_name = unquote(_first_header(self.request, b"x-device-name") or "Unknown Device")
        user_name = unquote(_first_header(self.request, b"x-user-name") or "Unknown User")

        ok, error = self.servicer._finalize_uploaded_file(
            self.request.owner_username, self.vault_id, self.current_rel_path,
            self.current_temp_path, self.current_file_path, self.total_bytes,
            self.modified_at_ms, calculated_hash, device_name, user_name,
            self.metadata_caches[self.vault_id], self.tombstone_indexes[self.vault_id],
        )

        self.current_rel_path = None
        self.current_temp_path = None
        self.current_file_path = None
        return _FrameResult(ack=(eof_payload.path, ok, error), reason=None if ok else "other")

    def _abort_blocking(self, _ignored):
        self._close_and_discard_current()
        return _FrameResult()

    def _close_and_discard_current(self) -> None:
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None
        if self.current_temp_path and os.path.exists(self.current_temp_path):
            os.remove(self.current_temp_path)
        self.current_rel_path = None
        self.current_temp_path = None
        self.current_file_path = None


def _first_header(request: Request, name: bytes) -> str | None:
    raw = request.requestHeaders.getRawHeaders(name)
    return raw[0].decode("utf-8", errors="replace") if raw else None


class StreamingUploadResource(Resource):
    """Handles /obsidian.sync.v1.SyncService/UploadFilesStream -- unlike SyncServiceResource
    (which fully buffers request.content before any application code runs), this path's
    body is parsed incrementally by StreamingUploadRequest, and render_POST here only needs
    to finish the response once the whole (already-processed) body has arrived."""

    isLeaf = True

    def __init__(self, servicer, upload_metrics: StreamingUploadMetrics | None = None):
        super().__init__()
        self.servicer = servicer
        self.metrics = upload_metrics if upload_metrics is not None else metrics

    def make_on_frame(self):
        def on_frame(request: Request, flags: int, payload: bytes) -> None:
            accumulator = getattr(request, "_upload_accumulator", None)
            if accumulator is None:
                accumulator = _UploadAccumulator(request, self.servicer, self.metrics)
                request._upload_accumulator = accumulator
            accumulator.handle_frame(flags, payload)

        return on_frame

    def render_POST(self, request: Request) -> bytes:
        accumulator = getattr(request, "_upload_accumulator", None)
        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"application/grpc-web+proto")
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")

        def _finish(_ignored=None):
            if accumulator is not None:
                for path, ok, error in accumulator.pending_acks:
                    ack = sync_pb2.UploadAck(path=path, ok=ok, error=error)
                    request.write(wrap_message(False, ack.SerializeToString()))
                logger.info(
                    "streaming upload finished: %d file(s) acked",
                    len(accumulator.pending_acks),
                )
            request.write(wrap_message(True, b""))
            if not request.finished:
                request.finish()

        if accumulator is not None:
            accumulator._chain.addCallback(_finish)
        else:
            _finish()
        return NOT_DONE_YET

    def render_OPTIONS(self, request: Request) -> bytes:
        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(204)
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Methods", b"POST, OPTIONS")
        request.setHeader(
            b"Access-Control-Allow-Headers",
            b"authorization, content-type, x-device-name, x-user-name",
        )
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")
        request.setHeader(b"Content-Length", b"0")
        return b""
