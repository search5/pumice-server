"""TDD for SyncServiceServicer.GetFileHistory/RestoreHistoryVersion speaking wire_types
instead of sync_pb2 -- part of PR5 (see pumice's 13_PR5_계획.md, option A). The last two of
the six RPC methods to convert (Delta: see test_service_delta.py; UploadFiles/DownloadFiles/
DownloadHistoryVersion: see test_service_upload_files.py etc.).

End-to-end coverage of the WS history_req/restore_req round trip already lives in
test_ws_sync_rpc_ops.py::test_history_then_restore_round_trip and is unaffected (JSON only,
never touches sync_pb2/wire_types directly). This file adds direct unit tests of the two RPC
methods themselves, which didn't exist before -- same gap pattern as the other conversions.
"""

import asyncio

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

    async def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")


def make_servicer_with_one_history_entry(tmp_path, repo):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_text("v1")
    # First scan of a newly-discovered file creates a history backup (see _scan_and_merge).
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")
    return servicer, vault_path


def test_get_file_history_returns_wire_types_history_response(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)

    request = wire_types.HistoryRequest(vault_id="vault1", path="note.md")
    response = asyncio.run(servicer.GetFileHistory(request, FakeContext()))

    assert isinstance(response, wire_types.HistoryResponse)
    assert len(response.versions) == 1
    version = response.versions[0]
    assert isinstance(version, wire_types.HistoryVersion)
    assert version.content_hash


def test_get_file_history_for_unknown_path_returns_empty_versions(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)

    request = wire_types.HistoryRequest(vault_id="vault1", path="nope.md")
    response = asyncio.run(servicer.GetFileHistory(request, FakeContext()))

    assert isinstance(response, wire_types.HistoryResponse)
    assert response.versions == []


def test_restore_history_version_returns_wire_types_response_and_restores_content(tmp_path, repo):
    servicer, vault_path = make_servicer_with_one_history_entry(tmp_path, repo)
    note = vault_path / "note.md"

    history = asyncio.run(servicer.GetFileHistory(
        wire_types.HistoryRequest(vault_id="vault1", path="note.md"), FakeContext()))
    history_id = history.versions[0].history_id

    # Overwrite via temp-file + rename (matches how real vault writes work -- see the ws
    # rpc-ops test's comment on why an in-place write would corrupt the hard-linked backup).
    tmp_note = note.with_suffix(".md.tmp")
    tmp_note.write_text("v2 -- overwritten locally")
    tmp_note.replace(note)

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=history_id)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert isinstance(response, wire_types.RestoreHistoryResponse)
    assert response.ok is True
    assert response.error == ""
    assert note.read_text() == "v1"


def test_restore_history_version_not_found_returns_wire_types_error_response(tmp_path, repo):
    servicer, _ = make_servicer_with_one_history_entry(tmp_path, repo)

    request = wire_types.RestoreHistoryRequest(vault_id="vault1", path="note.md", history_id=999999)
    response = asyncio.run(servicer.RestoreHistoryVersion(request, FakeContext()))

    assert isinstance(response, wire_types.RestoreHistoryResponse)
    assert response.ok is False
    assert response.error == "History version not found"
