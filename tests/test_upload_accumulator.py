"""TDD for _UploadAccumulator: turns EnvelopeStreamParser-decoded FileChunk frames into
actual (threaded, backpressure-guarded) disk writes, reusing
SyncServiceServicer._finalize_uploaded_file on EOF.

These tests need a real, running reactor (not a bare StringTransport-driven synchronous
call) because _UploadAccumulator dispatches its blocking work via
twisted.internet.threads.deferToThread, which requires reactor.getThreadPool() to actually
be alive -- hence pytest-twisted (a test function returning a Deferred is driven by a real
reactor until that Deferred fires). This is the same tradeoff test_real_socket.py in the
twisted-streaming-poc project made for the same reason.

The servicer/repository here are lightweight fakes (not the real SqlAlchemy-backed
SyncServiceServicer) so these tests stay fast and focused on accumulator behaviour --
end-to-end coverage against the real servicer lives in test_streaming_upload_integration.py.
"""

import hashlib
import threading

from twisted.internet.testing import StringTransport
from twisted.web import http

from pumice_server import sync_pb2
from pumice_server.streaming_upload_resource import StreamingUploadMetrics, _UploadAccumulator


class _FakeRepository:
    def __init__(self):
        self.load_all_calls = []

    def load_all(self, owner_username, vault_id):
        self.load_all_calls.append((owner_username, vault_id, threading.current_thread().name))
        return {}


class _FakeServicer:
    def __init__(self, data_dir, vault_root):
        self.data_dir = data_dir
        self.vault_root = vault_root
        self.repository = _FakeRepository()
        self.finalize_calls = []

    def _get_vault_path(self, owner_username, vault_id):
        import os

        return os.path.join(self.vault_root, owner_username, vault_id)

    def _finalize_uploaded_file(
        self, owner_username, vault_id, rel_path, temp_path, file_path,
        total_bytes, modified_at_ms, content_hash, device_name, user_name,
        metadata_cache, tombstones_by_hash,
    ):
        import os

        self.finalize_calls.append({
            "owner_username": owner_username,
            "vault_id": vault_id,
            "rel_path": rel_path,
            "content_hash": content_hash,
            "device_name": device_name,
            "user_name": user_name,
            "thread_name": threading.current_thread().name,
        })
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        os.rename(temp_path, file_path)
        return True, ""


class _FakeRequest:
    def __init__(self, owner_username):
        self.owner_username = owner_username
        self.transport = StringTransport()
        self.requestHeaders = http.Headers()
        self.requestHeaders.addRawHeader(b"x-device-name", b"Test%20Device")
        self.requestHeaders.addRawHeader(b"x-user-name", b"Test%20User")
        self.written = []

    def write(self, data):
        self.written.append(data)


def _pending_acks(accumulator) -> list:
    # pending_acks is (path, ok, error) tuples -- see _UploadAccumulator's module docstring
    # for why acks are buffered here rather than written to the request immediately: this is
    # a plain HTTP/1.1 connection, so the response can't start until render_POST() runs
    # (i.e. after the whole request body has arrived), no matter how early any individual
    # file finishes uploading.
    return [
        sync_pb2.UploadAck(path=path, ok=ok, error=error)
        for path, ok, error in accumulator.pending_acks
    ]


def test_full_file_upload_writes_to_disk_and_acks_via_finalize(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    accumulator = _UploadAccumulator(request, servicer)

    content = b"hello streamed world"
    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())

    data = sync_pb2.ChunkData(path="note.md", sequence=0, data=content)
    accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())

    eof = sync_pb2.ChunkEOF(path="note.md", content_hash=hashlib.sha256(content).hexdigest())
    accumulator.handle_frame(0x80, sync_pb2.FileChunk(eof=eof).SerializeToString())

    def _check(_ignored):
        assert len(servicer.finalize_calls) == 1
        call = servicer.finalize_calls[0]
        assert call["rel_path"] == "note.md"
        assert call["owner_username"] == "alice"
        assert call["device_name"] == "Test Device"
        assert call["user_name"] == "Test User"

        acks = _pending_acks(accumulator)
        assert len(acks) == 1
        assert acks[0].path == "note.md"
        assert acks[0].ok is True

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_hash_mismatch_is_rejected_without_calling_finalize(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    accumulator = _UploadAccumulator(request, servicer)

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    data = sync_pb2.ChunkData(path="note.md", sequence=0, data=b"hello")
    accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())
    eof = sync_pb2.ChunkEOF(path="note.md", content_hash="not-the-real-hash")
    accumulator.handle_frame(0x80, sync_pb2.FileChunk(eof=eof).SerializeToString())

    def _check(_ignored):
        assert servicer.finalize_calls == []
        acks = _pending_acks(accumulator)
        assert len(acks) == 1
        assert acks[0].ok is False
        assert "Hash verification failed" in acks[0].error

    accumulator._chain.addCallback(_check)
    return accumulator._chain


# --- Backpressure -----------------------------------------------------------------------


def test_transport_is_paused_synchronously_as_soon_as_a_frame_is_enqueued(tmp_path):
    """Backpressure must take effect the instant work is queued, not only once the worker
    thread actually starts running it -- otherwise a burst of frames delivered in one
    dataReceived() call (see StreamingUploadRequest/EnvelopeStreamParser, which doesn't
    yield between already-buffered complete frames) could all get enqueued before the
    reactor ever gets a chance to apply backpressure."""
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    accumulator = _UploadAccumulator(request, servicer)

    assert request.transport.producerState == "producing"

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())

    # No yield to the reactor has happened yet -- the pause must already be visible.
    assert request.transport.producerState == "paused"

    def _check(_ignored):
        assert request.transport.producerState == "producing"

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_transport_stays_paused_across_a_burst_of_frames_until_the_last_one_drains(tmp_path):
    """Multiple frames enqueued back to back (inflight count > 1) must not resume the
    transport after just the first one finishes -- only once the whole backlog drains."""
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    accumulator = _UploadAccumulator(request, servicer)

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=10, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    for i in range(5):
        data = sync_pb2.ChunkData(path="note.md", sequence=i, data=b"xx")
        accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())

    assert accumulator._inflight == 6
    assert request.transport.producerState == "paused"

    def _check(_ignored):
        assert accumulator._inflight == 0
        assert request.transport.producerState == "producing"

    accumulator._chain.addCallback(_check)
    return accumulator._chain


# --- Blocking I/O avoidance --------------------------------------------------------------


def test_disk_and_repository_work_runs_off_the_reactor_thread(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    accumulator = _UploadAccumulator(request, servicer)

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    data = sync_pb2.ChunkData(path="note.md", sequence=0, data=b"hello")
    accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())
    eof = sync_pb2.ChunkEOF(path="note.md", content_hash=hashlib.sha256(b"hello").hexdigest())
    accumulator.handle_frame(0x80, sync_pb2.FileChunk(eof=eof).SerializeToString())

    def _check(_ignored):
        reactor_thread_name = threading.current_thread().name
        assert servicer.repository.load_all_calls[0][2] != reactor_thread_name
        assert servicer.finalize_calls[0]["thread_name"] != reactor_thread_name

    accumulator._chain.addCallback(_check)
    return accumulator._chain


# --- Metrics ------------------------------------------------------------------------------


def test_successful_upload_records_bytes_and_file_success(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    upload_metrics = StreamingUploadMetrics()
    accumulator = _UploadAccumulator(request, servicer, upload_metrics)

    content = b"hello streamed world"
    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    data = sync_pb2.ChunkData(path="note.md", sequence=0, data=content)
    accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())
    eof = sync_pb2.ChunkEOF(path="note.md", content_hash=hashlib.sha256(content).hexdigest())
    accumulator.handle_frame(0x80, sync_pb2.FileChunk(eof=eof).SerializeToString())

    def _check(_ignored):
        assert upload_metrics.total_bytes_received == len(content)
        assert upload_metrics.total_files_succeeded == 1
        assert upload_metrics.total_files_failed == 0

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_hash_mismatch_is_recorded_as_a_file_failure_with_reason(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    upload_metrics = StreamingUploadMetrics()
    accumulator = _UploadAccumulator(request, servicer, upload_metrics)

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    data = sync_pb2.ChunkData(path="note.md", sequence=0, data=b"hello")
    accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())
    eof = sync_pb2.ChunkEOF(path="note.md", content_hash="not-the-real-hash")
    accumulator.handle_frame(0x80, sync_pb2.FileChunk(eof=eof).SerializeToString())

    def _check(_ignored):
        assert upload_metrics.total_files_succeeded == 0
        assert upload_metrics.total_files_failed == 1
        assert upload_metrics.file_failure_reasons["hash_mismatch"] == 1

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_invalid_vault_id_is_recorded_with_its_own_reason(tmp_path):
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))

    class _RaisingServicer(_FakeServicer):
        def _get_vault_path(self, owner_username, vault_id):
            raise ValueError("nope")

    raising_servicer = _RaisingServicer(data_dir=servicer.data_dir, vault_root=servicer.vault_root)
    request = _FakeRequest(owner_username="alice")
    upload_metrics = StreamingUploadMetrics()
    accumulator = _UploadAccumulator(request, raising_servicer, upload_metrics)

    header = sync_pb2.ChunkHeader(vault_id="bad-vault", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())

    def _check(_ignored):
        assert upload_metrics.total_files_failed == 1
        assert upload_metrics.file_failure_reasons["invalid_vault"] == 1

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_backpressure_pause_is_recorded_once_per_drain_not_per_frame(tmp_path):
    """A burst of 6 enqueued frames (1 header + 5 data) is one continuous pause window, not
    six separate ones -- see _enqueue()'s _inflight counter."""
    servicer = _FakeServicer(data_dir=str(tmp_path / "data"), vault_root=str(tmp_path / "vaults"))
    request = _FakeRequest(owner_username="alice")
    upload_metrics = StreamingUploadMetrics()
    accumulator = _UploadAccumulator(request, servicer, upload_metrics)

    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=10, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())
    for i in range(5):
        data = sync_pb2.ChunkData(path="note.md", sequence=i, data=b"xx")
        accumulator.handle_frame(0, sync_pb2.FileChunk(data=data).SerializeToString())

    def _check(_ignored):
        assert upload_metrics.backpressure_pause_events == 1
        assert upload_metrics.backpressure_paused_seconds_total >= 0.0

    accumulator._chain.addCallback(_check)
    return accumulator._chain


def test_unhandled_exception_in_blocking_handler_still_resumes_transport():
    """Regression test for a real deadlock risk: if a blocking handler raises something
    that isn't already converted into a _FrameResult (e.g. an OSError writing to disk), the
    inflight/resumeProducing bookkeeping in _enqueue()'s _after() must still run -- an
    earlier version used addCallback (skipped on failure) for that bookkeeping and
    addErrback separately only for logging, which left the transport paused forever."""
    request = _FakeRequest(owner_username="alice")
    upload_metrics = StreamingUploadMetrics()

    class _BrokenServicer:
        data_dir = "/nonexistent"

        class repository:
            @staticmethod
            def load_all(owner_username, vault_id):
                raise RuntimeError("simulated unexpected failure")

    accumulator = _UploadAccumulator(request, _BrokenServicer(), upload_metrics)
    header = sync_pb2.ChunkHeader(vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)
    accumulator.handle_frame(0, sync_pb2.FileChunk(header=header).SerializeToString())

    assert request.transport.producerState == "paused"

    def _check(_ignored):
        assert request.transport.producerState == "producing"
        assert accumulator._inflight == 0
        assert upload_metrics.total_files_failed == 1
        assert upload_metrics.file_failure_reasons["other"] == 1

    accumulator._chain.addCallback(_check)
    return accumulator._chain
