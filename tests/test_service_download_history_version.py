"""TDD for SyncServiceServicer.DownloadHistoryVersion speaking wire_types instead of sync_pb2
-- part of PR5 (see pumice's 13_PR5_계획.md). Shares FileChunk/ChunkHeader/ChunkData/ChunkEOF
with UploadFiles/DownloadFiles (see test_service_upload_files.py, test_service_download_files.py).

End-to-end coverage of the WS history_dl_req path already lives in
test_ws_sync_streaming_ops.py's test_history_dl_req_streams_a_backed_up_version and is
unaffected (it only asserts on the raw bytes received, not the intermediate wire type). This
file adds a direct unit test of DownloadHistoryVersion() itself.
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
        return [("authorization", f"Bearer {self._token}")]

    def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")


async def _collect(agen):
    return [x async for x in agen]


def test_download_history_version_yields_wire_types_file_chunks(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    content = b"version 1 content"
    (vault_path / "note.md").write_bytes(content)

    servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    history_rows = repo.get_history("alice", "vault1", "note.md")
    assert len(history_rows) == 1
    history_id = history_rows[0]["history_id"]

    request = wire_types.HistoryVersionDownloadRequest(vault_id="vault1", path="note.md", history_id=history_id)

    chunks = asyncio.run(_collect(servicer.DownloadHistoryVersion(request, FakeContext())))

    assert all(isinstance(c, wire_types.FileChunk) for c in chunks)
    kinds = [c.WhichOneof("payload") for c in chunks]
    assert kinds[0] == "header"
    assert kinds[-1] == "eof"
    data_bytes = b"".join(c.data.data for c in chunks if c.data is not None)
    assert data_bytes == content
    eof = chunks[-1].eof
    assert eof.content_hash == hashlib.sha256(content).hexdigest()
