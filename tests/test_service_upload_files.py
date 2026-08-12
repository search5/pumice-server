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
