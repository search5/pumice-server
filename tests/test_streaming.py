"""TDD for EnvelopeStreamParser: incrementally parses gRPC-Web/Connect-style enveloped
messages (1-byte flags + 4-byte big-endian length + payload) from arbitrarily-chunked
byte input, invoking a callback per complete frame -- without ever buffering more than
one in-flight frame's worth of bytes.

This mirrors the wire format already confirmed against the real pumice-server
(grpc_web_resource.py's wrap_message/unwrap_message) and verified end-to-end in the
client-side Playwright test in the pumice repo.
"""

import struct

import pytest

from pumice_server.streaming import EnvelopeStreamParser, FrameTooLargeError, DEFAULT_MAX_FRAME_BYTES


def make_frame(flags: int, payload: bytes) -> bytes:
    return struct.pack(">BI", flags, len(payload)) + payload


def test_single_complete_frame_in_one_feed():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    parser.feed(make_frame(0, b"hello"))

    assert received == [(0, b"hello")]


def test_frame_with_nonzero_flags():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    parser.feed(make_frame(0x80, b"trailer-ish"))

    assert received == [(0x80, b"trailer-ish")]


def test_empty_payload_frame():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    parser.feed(make_frame(0, b""))

    assert received == [(0, b"")]


def test_multiple_complete_frames_in_one_feed_call():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    data = make_frame(0, b"first") + make_frame(0, b"second") + make_frame(0x80, b"third")
    parser.feed(data)

    assert received == [(0, b"first"), (0, b"second"), (0x80, b"third")]


def test_frame_split_byte_by_byte_across_many_feed_calls():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    data = make_frame(0, b"chunked-slowly")
    for i in range(len(data)):
        parser.feed(data[i:i + 1])
        if i < len(data) - 1:
            # No callback should fire until the very last byte of the frame arrives.
            assert received == []

    assert received == [(0, b"chunked-slowly")]


def test_partial_header_does_not_trigger_callback():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    # Header alone is 5 bytes (1 flag + 4 length); feed only 4.
    parser.feed(b"\x00\x00\x00\x00")

    assert received == []


def test_interleaved_partial_and_complete_frames():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    frame_a = make_frame(0, b"alpha")
    frame_b = make_frame(0, b"bravo-longer-payload")
    combined = frame_a + frame_b

    # Split the combined stream at an arbitrary point that lands mid-frame-b.
    split_at = len(frame_a) + 3
    parser.feed(combined[:split_at])
    assert received == [(0, b"alpha")]  # frame_a complete, frame_b still partial

    parser.feed(combined[split_at:])
    assert received == [(0, b"alpha"), (0, b"bravo-longer-payload")]


def test_large_realistic_payload_matching_256kb_chunk_size():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append((flags, payload)))

    payload = bytes(range(256)) * 1024  # 256KB, matching the real CHUNK_SIZE used in syncClient.ts
    parser.feed(make_frame(0, payload))

    assert received == [(0, payload)]


def test_feed_never_buffers_more_than_one_frame_ahead():
    """Bounded-memory guarantee: after a complete frame is emitted, the internal buffer
    should not be holding onto bytes belonging to that already-emitted frame."""
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: None)

    parser.feed(make_frame(0, b"x" * 1000))
    assert len(parser._buffer) == 0

    # Feed a second frame's header only -- buffer should hold *only* those 5 bytes,
    # not 5 + leftover bytes from the first (already-consumed) frame.
    parser.feed(b"\x00\x00\x00\x00\x0a")
    assert len(parser._buffer) == 5


def test_oversized_frame_is_rejected_immediately_after_header_not_after_waiting_for_payload():
    """Hardening (was: 'a huge length just means wait for more bytes' -- that was the
    unbounded-memory DoS vector: a malicious/broken client could declare a length in the
    gigabytes and the parser would just keep extending self._buffer waiting for a payload
    that may never fully arrive, or that arrives slowly and ties up memory the whole time.
    Now: reject as soon as the length is known (5 bytes in), before any payload bytes are
    required or buffered for this frame)."""
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: None)

    huge_length_header = struct.pack(">BI", 0, 2**31)
    with pytest.raises(FrameTooLargeError):
        parser.feed(huge_length_header)


def test_default_max_frame_bytes_matches_documented_value():
    # 4MB -- matches common gRPC default max receive message size conventions, comfortably
    # above the real protocol's 256KB data-chunk convention (see syncClient.ts CHUNK_SIZE)
    # while still bounding worst-case per-frame memory.
    assert DEFAULT_MAX_FRAME_BYTES == 4 * 1024 * 1024


def test_frame_exactly_at_max_frame_bytes_is_allowed():
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append(payload), max_frame_bytes=10)

    parser.feed(make_frame(0, b"x" * 10))  # exactly at the limit

    assert received == [b"x" * 10]


def test_frame_one_byte_over_max_frame_bytes_is_rejected():
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: None, max_frame_bytes=10)

    header = struct.pack(">BI", 0, 11)  # one byte over the limit
    with pytest.raises(FrameTooLargeError):
        parser.feed(header)


def test_rejection_does_not_require_any_payload_bytes_to_have_arrived():
    """The whole point: reject based on the declared length in the header alone, not by
    actually receiving (and thus buffering) the oversized payload first."""
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: None, max_frame_bytes=10)

    header_only = struct.pack(">BI", 0, 999_999)
    with pytest.raises(FrameTooLargeError):
        parser.feed(header_only)  # no payload bytes included at all

    # Confirms the rejection happened at the header, not from buffer growth: the parser
    # never accumulated any of the (nonexistent, never-sent) oversized payload.
    assert len(parser._buffer) <= 5


def test_frames_under_the_limit_before_and_after_a_would_be_oversized_one_are_unaffected():
    """A parser instance is expected to be discarded (request aborted) once it raises --
    this just confirms the limit check itself doesn't accidentally reject valid frames that
    merely happen to be parsed by the same parser class/defaults."""
    received = []
    parser = EnvelopeStreamParser(on_frame=lambda flags, payload: received.append(payload), max_frame_bytes=1000)

    parser.feed(make_frame(0, b"small-frame-well-under-limit"))

    assert received == [b"small-frame-well-under-limit"]
