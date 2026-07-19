"""TDD for StreamingUploadRequest: auth-before-streaming, backpressure (transport
pause/resume around token resolution), and blocking-I/O avoidance (token resolution
injected as a Deferred rather than called inline).

Driven against a real HTTPChannel + StringTransport, same deterministic pattern used by
the /home/jiho/twisted-streaming-poc test suite this was ported from. Deferreds are fired
manually (via defer.succeed/fail or a held-open Deferred) rather than via a real thread
pool, so these tests stay synchronous and fast -- test_streaming_upload_resource_real_reactor.py
covers the real threads.deferToThread wiring end to end.
"""

import struct

import pytest
from twisted.internet import defer
from twisted.internet.error import ConnectionDone
from twisted.internet.task import Clock
from twisted.internet.testing import StringTransport
from twisted.python.failure import Failure
from twisted.web import http, resource, server

from pumice_server.streaming_upload_resource import (
    AUTH_TIMEOUT_SECONDS,
    MAX_UPLOAD_SECONDS,
    StreamingAuthError,
    StreamingUploadMetrics,
    make_streaming_upload_request_factory,
    resolve_owner_username,
)


def simulate_connection_lost(channel) -> None:
    """StringTransport.loseConnection() only flips a flag -- under a real reactor the OS
    eventually notices the socket closed and calls channel.connectionLost(), which is what
    actually fires Request.notifyFinish() (see StreamingUploadRequest's active_uploads
    tracking). Nothing drives that automatically here, so tests that need to observe
    post-abort cleanup (e.g. metrics.active_uploads decrementing) must simulate it
    explicitly -- confirmed empirically that loseConnection() alone does NOT fire
    notifyFinish() under StringTransport, only this does."""
    channel.connectionLost(Failure(ConnectionDone()))


def make_frame(flags: int, payload: bytes) -> bytes:
    return struct.pack(">BI", flags, len(payload)) + payload


class _NoOpLeafResource(resource.Resource):
    isLeaf = True

    def render_POST(self, request):
        return b""


def start_channel(
    should_stream, resolve_owner, on_frame, root_resource=None, clock=None, upload_metrics=None
):
    transport = StringTransport()
    channel = http.HTTPChannel()
    created_requests = []
    base_factory = make_streaming_upload_request_factory(
        should_stream, resolve_owner, on_frame, clock=clock or Clock(), upload_metrics=upload_metrics
    )

    def tracking_factory(chan, queued=0):
        request = base_factory(chan, queued)
        created_requests.append(request)
        return request

    channel.requestFactory = tracking_factory
    channel.site = server.Site(root_resource or _NoOpLeafResource())
    channel.makeConnection(transport)
    return channel, transport, created_requests


def send_request_head(channel, path: bytes, body_length: int, headers: bytes = b"") -> None:
    channel.dataReceived(
        b"POST " + path + b" HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Content-Length: " + str(body_length).encode("ascii") + b"\r\n"
        + headers
        + b"\r\n"
    )


# --- resolve_owner_username(): pure function, unit-tested in isolation ---------------------


class _FakeRequest:
    def __init__(self, auth_header: bytes | None):
        self.requestHeaders = http.Headers()
        if auth_header is not None:
            self.requestHeaders.addRawHeader(b"authorization", auth_header)


class _FakeRepository:
    def __init__(self, devices_by_token: dict):
        self._devices = devices_by_token

    def get_device_token(self, token):
        return self._devices.get(token)


def test_resolve_owner_username_missing_header_raises():
    with pytest.raises(StreamingAuthError):
        resolve_owner_username(_FakeRequest(None), _FakeRepository({}))


def test_resolve_owner_username_unknown_token_raises():
    with pytest.raises(StreamingAuthError):
        resolve_owner_username(_FakeRequest(b"Bearer nope"), _FakeRepository({}))


def test_resolve_owner_username_valid_bearer_token_returns_username():
    repo = _FakeRepository({"tok123": {"username": "alice"}})
    assert resolve_owner_username(_FakeRequest(b"Bearer tok123"), repo) == "alice"


def test_resolve_owner_username_accepts_bare_token_without_bearer_prefix():
    repo = _FakeRepository({"tok123": {"username": "alice"}})
    assert resolve_owner_username(_FakeRequest(b"tok123"), repo) == "alice"


# --- StreamingUploadRequest plumbing: auth-before-streaming --------------------------------


def test_valid_auth_allows_streaming_and_frames_are_parsed():
    received = []
    body = make_frame(0, b"hello-streamed")

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: received.append((flags, payload)),
    )

    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)

    assert received == [(0, b"hello-streamed")]
    assert created_requests[0].owner_username == "alice"


def test_invalid_auth_rejects_before_any_frame_is_parsed():
    received = []
    body = make_frame(0, b"should-never-be-parsed")

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.fail(StreamingAuthError("bad token")),
        on_frame=lambda request, flags, payload: received.append((flags, payload)),
    )

    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)  # must not raise

    assert received == []
    assert transport.disconnecting is True
    assert b"401" in transport.value()


def test_non_streaming_path_never_calls_resolve_owner():
    calls = []

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",  # this request's path won't match
        resolve_owner=lambda request: calls.append(1) or defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
    )

    body = b"plain body"
    send_request_head(channel, b"/not-streamed", len(body))
    channel.dataReceived(body)

    assert calls == []


# --- Backpressure: transport paused while auth resolves, resumed after ---------------------


def test_transport_is_paused_immediately_when_a_streaming_request_starts():
    held = defer.Deferred()  # never fires in this test -- we only care about the pause

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
    )

    send_request_head(channel, b"/stream", body_length=100)

    assert transport.producerState == "paused"


def test_transport_resumes_once_auth_resolves():
    held = defer.Deferred()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
    )

    send_request_head(channel, b"/stream", body_length=100)
    assert transport.producerState == "paused"

    held.callback("alice")

    assert transport.producerState == "producing"


class _RenderTrackingResource(resource.Resource):
    isLeaf = True

    def __init__(self, calls: list):
        super().__init__()
        self._calls = calls

    def render_POST(self, request):
        self._calls.append(request)
        return b""


def test_small_full_body_arriving_before_auth_resolves_does_not_race_render():
    """Regression test: pauseProducing() in gotLength() only stops *future* socket reads --
    it does nothing about bytes already delivered within the current dataReceived() call.
    A small enough body (all of it arriving in the same call as the headers) used to reach
    allContentReceived() -> requestReceived() -> process() -> render() *before* the auth
    Deferred had a chance to resolve, since resolving it requires a real round trip through
    the thread pool that can't complete synchronously within that same call. Caught by
    actually driving this end to end against a running server, not by reasoning about it."""
    render_calls: list = []
    held = defer.Deferred()
    body = make_frame(0, b"tiny")

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
        root_resource=_RenderTrackingResource(render_calls),
    )

    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)  # whole body arrives before auth resolves

    assert render_calls == []  # must not have rendered yet -- owner_username still unknown

    held.callback("alice")

    assert render_calls == [created_requests[0]]
    assert created_requests[0].owner_username == "alice"


def test_small_full_body_arriving_before_auth_fails_does_not_render_either():
    render_calls: list = []
    held = defer.Deferred()
    body = make_frame(0, b"tiny")

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
        root_resource=_RenderTrackingResource(render_calls),
    )

    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)
    assert render_calls == []

    held.errback(StreamingAuthError("bad token"))  # must not raise

    assert render_calls == []
    assert transport.disconnecting is True
    assert b"401" in transport.value()


def test_bytes_arriving_before_auth_resolves_are_buffered_then_processed():
    """A client that sends headers and the first body bytes in the same TCP segment can
    still have those bytes reach handleContentChunk() before pauseProducing() has any
    real-world effect (it only stops *future* socket reads) -- they must be buffered and
    replayed once auth resolves, not dropped or fed to a parser that doesn't exist yet."""
    received = []
    held = defer.Deferred()
    body = make_frame(0, b"arrived-early")

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: received.append(payload),
    )

    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)  # arrives while auth is still pending

    assert received == []  # not processed yet -- parser doesn't exist until auth resolves

    held.callback("alice")

    assert received == [b"arrived-early"]


# --- Timeouts ----------------------------------------------------------------------------


def test_auth_resolution_times_out_if_it_never_resolves():
    """A stuck DB thread call (pool exhaustion, deadlock, ...) must not leave the request --
    and its paused transport -- hanging forever. Deterministic via task.Clock: no real
    reactor/sleep needed to prove the timeout fires at exactly the right simulated time."""
    clock = Clock()
    held = defer.Deferred()  # never fires on its own -- simulates a stuck lookup

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
        clock=clock,
    )

    send_request_head(channel, b"/stream", body_length=10)
    assert transport.disconnecting is False

    clock.advance(AUTH_TIMEOUT_SECONDS - 0.001)
    assert transport.disconnecting is False  # not yet -- still within the timeout window

    clock.advance(0.002)
    assert transport.disconnecting is True
    assert b"503" in transport.value()  # distinct from 401: this is "we were too slow", not
    # "your credentials are wrong"


def test_auth_resolution_within_timeout_is_unaffected():
    clock = Clock()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        clock=clock,
    )

    send_request_head(channel, b"/stream", body_length=10)
    assert created_requests[0].owner_username == "alice"

    clock.advance(AUTH_TIMEOUT_SECONDS + 1)  # must be a no-op -- already resolved
    assert transport.disconnecting is False


def test_upload_taking_longer_than_max_duration_is_aborted():
    """Defends against a slow-loris style sender that drips bytes just often enough to keep
    resetting Twisted's own idle timeout (see StreamingUploadRequest's module docstring /
    pumice-server README's "타임아웃" section) but never actually finishes the body -- a pure
    idle timeout can't catch this since it only measures gaps between bytes, not total
    elapsed time. This is a *ceiling* independent of activity."""
    clock = Clock()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        clock=clock,
    )

    body = make_frame(0, b"first-frame-of-many-more-to-come")
    send_request_head(channel, b"/stream", body_length=1_000_000)  # declares far more to come
    channel.dataReceived(body)  # only a little arrives -- the rest never does
    assert transport.disconnecting is False

    clock.advance(MAX_UPLOAD_SECONDS + 1)

    assert transport.disconnecting is True
    assert b"408" in transport.value()


def test_upload_finishing_before_max_duration_cancels_the_timer():
    """The timer must not fire (or leak a pending DelayedCall) once the request has already
    finished normally -- checked two ways: no disconnect happens well past the deadline, and
    the reactor (Clock) has no dangling scheduled calls left over."""
    clock = Clock()
    render_calls: list = []

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        root_resource=_RenderTrackingResource(render_calls),
        clock=clock,
    )

    body = make_frame(0, b"whole-body-arrives-immediately")
    send_request_head(channel, b"/stream", len(body))
    channel.dataReceived(body)
    assert render_calls == [created_requests[0]]

    clock.advance(MAX_UPLOAD_SECONDS + 1)

    assert transport.disconnecting is False
    assert clock.getDelayedCalls() == []


def test_auth_failure_cancels_the_max_duration_timer_too():
    """An auth rejection is itself a form of abort -- must not leave the max-duration timer
    dangling to fire later against an already-closed connection."""
    clock = Clock()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.fail(StreamingAuthError("bad token")),
        on_frame=lambda request, flags, payload: None,
        clock=clock,
    )

    send_request_head(channel, b"/stream", body_length=10)
    assert transport.disconnecting is True  # rejected immediately

    assert clock.getDelayedCalls() == []


# --- Metrics -------------------------------------------------------------------------------


def test_active_uploads_increments_on_start_and_decrements_on_normal_finish():
    upload_metrics = StreamingUploadMetrics()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        upload_metrics=upload_metrics,
    )

    # gotLength() runs as soon as headers are parsed, before any body byte has arrived --
    # active_uploads must already reflect the in-progress request at that point.
    send_request_head(channel, b"/stream", body_length=5)
    assert upload_metrics.active_uploads == 1
    assert upload_metrics.total_uploads_started == 1

    # Under StringTransport, a small/fast request's entire lifecycle -- including
    # finish()/_cleanup(), which is what actually fires notifyFinish() -- runs
    # synchronously to completion inside this single dataReceived() call (no real reactor
    # loop needed to drive it, unlike the loseConnection()-only abort paths -- see
    # simulate_connection_lost()'s docstring for that contrast).
    channel.dataReceived(make_frame(0, b"hello"))

    assert upload_metrics.active_uploads == 0
    assert upload_metrics.total_uploads_started == 1  # unaffected by finishing


def test_active_uploads_decrements_after_auth_rejection_too():
    upload_metrics = StreamingUploadMetrics()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.fail(StreamingAuthError("bad token")),
        on_frame=lambda request, flags, payload: None,
        upload_metrics=upload_metrics,
    )

    send_request_head(channel, b"/stream", body_length=10)
    assert upload_metrics.active_uploads == 1  # started before auth resolved
    assert upload_metrics.rejections["auth_failed"] == 1

    simulate_connection_lost(channel)

    assert upload_metrics.active_uploads == 0


def test_non_streaming_requests_never_touch_upload_metrics():
    upload_metrics = StreamingUploadMetrics()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        upload_metrics=upload_metrics,
    )

    body = b"plain body"
    send_request_head(channel, b"/not-streamed", len(body))
    channel.dataReceived(body)
    simulate_connection_lost(channel)

    assert upload_metrics.total_uploads_started == 0
    assert upload_metrics.active_uploads == 0


def test_auth_timeout_is_recorded_under_its_own_rejection_reason():
    clock = Clock()
    upload_metrics = StreamingUploadMetrics()
    held = defer.Deferred()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: held,
        on_frame=lambda request, flags, payload: None,
        clock=clock,
        upload_metrics=upload_metrics,
    )

    send_request_head(channel, b"/stream", body_length=10)
    clock.advance(AUTH_TIMEOUT_SECONDS + 1)

    assert upload_metrics.rejections["auth_timeout"] == 1
    assert upload_metrics.rejections["auth_failed"] == 0


def test_max_duration_exceeded_is_recorded_under_its_own_rejection_reason():
    clock = Clock()
    upload_metrics = StreamingUploadMetrics()

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        clock=clock,
        upload_metrics=upload_metrics,
    )

    send_request_head(channel, b"/stream", body_length=1_000_000)
    channel.dataReceived(make_frame(0, b"only-a-little"))
    clock.advance(MAX_UPLOAD_SECONDS + 1)

    assert upload_metrics.rejections["max_duration_exceeded"] == 1


def test_frame_too_large_and_malformed_frame_are_bucketed_separately():
    from pumice_server.streaming import DEFAULT_MAX_FRAME_BYTES

    upload_metrics_a = StreamingUploadMetrics()
    oversized_header = struct.pack(">BI", 0, DEFAULT_MAX_FRAME_BYTES + 1)
    channel_a, _, _ = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
        upload_metrics=upload_metrics_a,
    )
    send_request_head(channel_a, b"/stream", len(oversized_header))
    channel_a.dataReceived(oversized_header)
    assert upload_metrics_a.rejections["frame_too_large"] == 1
    assert upload_metrics_a.rejections["malformed_frame"] == 0

    upload_metrics_b = StreamingUploadMetrics()

    def broken_on_frame(request, flags, payload):
        raise RuntimeError("boom")

    body = make_frame(0, b"triggers-the-bug")
    channel_b, _, _ = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: defer.succeed("alice"),
        on_frame=broken_on_frame,
        upload_metrics=upload_metrics_b,
    )
    send_request_head(channel_b, b"/stream", len(body))
    channel_b.dataReceived(body)
    assert upload_metrics_b.rejections["malformed_frame"] == 1
    assert upload_metrics_b.rejections["frame_too_large"] == 0


# --- CORS preflight -----------------------------------------------------------------------


def test_options_preflight_bypasses_streaming_auth_gate():
    """Regression test: found via a real browser CORS preflight against a real running server,
    not via these StringTransport tests -- an OPTIONS request to the streaming path was being
    treated as an auth-gated streaming body. Since CORS preflights never carry Authorization
    (by spec), resolve_owner() always failed it with 401, and _abort() writes a bare status
    line with no CORS headers at all -- breaking the preflight itself ("No
    'Access-Control-Allow-Origin' header is present" in the browser, instead of ever reaching
    render_OPTIONS())."""
    calls: list = []

    channel, transport, created_requests = start_channel(
        should_stream=lambda path: path == b"/stream",
        resolve_owner=lambda request: calls.append(1) or defer.succeed("alice"),
        on_frame=lambda request, flags, payload: None,
    )

    channel.dataReceived(
        b"OPTIONS /stream HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Origin: null\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )

    assert calls == []  # resolve_owner must never be called for a preflight
    assert transport.disconnecting is False  # not force-aborted like the auth-401 path is
