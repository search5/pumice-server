"""Incremental parser for gRPC-Web/Connect-style enveloped messages.

Wire format (matches grpc_web_resource.py's wrap_message/unwrap_message):
    1 byte  flags (bit 0x80 = trailer frame)
    4 bytes big-endian payload length
    N bytes payload

Designed to be fed arbitrarily-sized chunks of bytes as they arrive off a socket --
never assumes a chunk boundary lines up with a frame boundary -- and to hold at most
one in-flight (incomplete) frame's worth of bytes in memory at a time, *bounded* by
max_frame_bytes (see FrameTooLargeError) rather than unconditionally growing to
whatever length a frame header claims.

Ported from the TDD PoC at /home/jiho/twisted-streaming-poc (src/streaming_poc/envelope.py),
where this design (including the DoS-hardening max_frame_bytes check) was verified against
28 tests covering plain parsing, chunk-boundary splitting, and oversized-frame rejection.
"""

from __future__ import annotations

import struct
from typing import Callable

_HEADER_FORMAT = ">BI"
_HEADER_LENGTH = struct.calcsize(_HEADER_FORMAT)

# 4MB: matches common gRPC default max receive message size conventions, comfortably above
# the real protocol's 256KB data-chunk convention (see syncClient.ts's CHUNK_SIZE) while
# still bounding worst-case per-frame memory against a malicious or broken client declaring
# an enormous length and either never finishing the payload or slowly dripping it in.
DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024

OnFrame = Callable[[int, bytes], None]


class FrameTooLargeError(ValueError):
    """Raised as soon as a frame's declared length (from its 5-byte header) exceeds
    max_frame_bytes -- before any of that frame's payload bytes are required to have
    arrived, let alone buffered. The parser instance should be treated as done/unusable
    after this: the caller is expected to abort the request/connection, not keep feeding it.
    """

    def __init__(self, declared_length: int, max_frame_bytes: int) -> None:
        super().__init__(
            f"frame declares {declared_length} bytes, exceeding the {max_frame_bytes}-byte limit"
        )
        self.declared_length = declared_length
        self.max_frame_bytes = max_frame_bytes


class EnvelopeStreamParser:
    def __init__(self, on_frame: OnFrame, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        self._on_frame = on_frame
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            if len(self._buffer) < _HEADER_LENGTH:
                return
            flags, length = struct.unpack(_HEADER_FORMAT, self._buffer[:_HEADER_LENGTH])
            if length > self._max_frame_bytes:
                # Deliberately raised before touching payload bytes: we don't want to have
                # already buffered any of an oversized frame just to then reject it.
                raise FrameTooLargeError(length, self._max_frame_bytes)
            frame_end = _HEADER_LENGTH + length
            if len(self._buffer) < frame_end:
                return
            payload = bytes(self._buffer[_HEADER_LENGTH:frame_end])
            del self._buffer[:frame_end]
            self._on_frame(flags, payload)
