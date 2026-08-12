"""TDD for SyncServiceServicer.UploadFiles speaking wire_types instead of sync_pb2 -- part of
PR5 (see pumice's 13_PR5_계획.md, option A: full protobuf removal).

End-to-end coverage of the WS upload path (through SyncWsSession, JSON in -> UploadAck JSON
out) already lives in test_ws_sync_streaming_ops.py and is unaffected by this change (it never
touches sync_pb2/wire_types directly). This file adds what didn't exist before: a direct unit
test of UploadFiles() itself, asserting on the wire_types boundary specifically.
"""

import asyncio
import hashlib

import pytest

from pumice_server import wire_types
from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakeContext:
    def __init__(self, token="tok"):
        self._token = token

    def invocation_metadata(self):
        return [
            ("authorization", f"Bearer {self._token}"),
            ("x-device-name", "test-device"),
            ("x-user-name", "test-user"),
        ]


async def _collect(agen):
    return [x async for x in agen]


def test_upload_files_accepts_and_yields_wire_types(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"hello upload"
    content_hash = hashlib.sha256(content).hexdigest()

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000)),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=content)),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash=content_hash)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert isinstance(acks[0], wire_types.UploadAck)
    assert acks[0].path == "note.md"
    assert acks[0].ok is True
    assert acks[0].error == ""

    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert written.read_bytes() == content


def test_upload_files_hash_mismatch_is_acked_as_a_failure(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=5, modified_at_ms=1000)),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=b"hello")),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash="not-the-real-hash")),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert isinstance(acks[0], wire_types.UploadAck)
    assert acks[0].ok is False
    assert "Hash verification failed" in acks[0].error


def test_upload_files_rejects_a_file_over_the_configured_max_size(tmp_path, repo):
    """PR5 follow-up (item 5, 2026-08): mirrors Obsidian core's own Sync client, which has the
    server advertise a perFileMax the client can check up front. Here the enforcement itself is
    server-side -- see test_ws_sync_session.py for the init_ok advertisement half."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, max_file_size_bytes=10)

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="big.md", total_bytes=11, modified_at_ms=1000)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert acks[0].ok is False
    assert "too large" in acks[0].error.lower()
    # Rejected before any temp file was ever created for it.
    assert not (tmp_path / "tmp").exists()


def test_upload_files_accepts_a_file_exactly_at_the_configured_max_size(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, max_file_size_bytes=5)
    content = b"hello"
    content_hash = hashlib.sha256(content).hexdigest()

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000)),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=content)),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash=content_hash)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert acks[0].ok is True


def test_upload_files_accepts_a_header_content_hash_matching_the_eof_hash(tmp_path, repo):
    """PR5 follow-up (item 6, 2026-08): the client now declares contentHash up front in the
    header too (not just at eof) -- laying groundwork for a future dedup-skip optimization
    (see #11_websocket_동기화_프로토콜_설계.md's re-analysis of Obsidian core's own push op,
    which sends the hash before any bytes transfer). No dedup logic yet -- this just accepts
    the extra field and expects it to agree with the eof hash."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"hello"
    content_hash = hashlib.sha256(content).hexdigest()

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000,
            content_hash=content_hash)),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=content)),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash=content_hash)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert acks[0].ok is True


def test_upload_files_rejects_when_header_hash_disagrees_with_eof_hash(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"hello"
    real_hash = hashlib.sha256(content).hexdigest()

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000,
            content_hash="a-different-hash-than-eof-declares")),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=content)),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash=real_hash)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert acks[0].ok is False
    assert "header" in acks[0].error.lower()


def test_upload_files_treats_an_absent_header_hash_as_no_constraint(tmp_path, repo):
    """Backward-compatible default: a header with no contentHash at all (content_hash="") skips
    the consistency check entirely -- matches wire_types.ChunkHeader's default."""
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    content = b"hello"
    real_hash = hashlib.sha256(content).hexdigest()

    request = wire_types.UploadBatch(chunks=[
        wire_types.FileChunk(header=wire_types.ChunkHeader(
            vault_id="vault1", path="note.md", total_bytes=len(content), modified_at_ms=1000)),
        wire_types.FileChunk(data=wire_types.ChunkData(path="note.md", sequence=0, data=content)),
        wire_types.FileChunk(eof=wire_types.ChunkEOF(path="note.md", content_hash=real_hash)),
    ])

    acks = asyncio.run(_collect(servicer.UploadFiles(request, FakeContext())))

    assert len(acks) == 1
    assert acks[0].ok is True
