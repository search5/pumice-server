"""TDD for the binary file-chunk streaming ops (upload/download/history-download) -- the
remainder of PR2 in #11_websocket_동기화_프로토콜_설계.md.

Design note (not in the original doc, added while implementing): raw binary WS frames carry no
envelope at all (matching Obsidian core's framing -- see the design doc's "왜 JSON인가"
section), so the server can only ever have ONE streaming op (upload OR download OR
history-download) actively producing/consuming binary frames on a connection at a time.
SyncWsSession enforces this with a single _stream_active flag. There is also no requestId in
the protocol anywhere anymore (see #14_옵시디언싱크_정렬_구현계획.md in the pumice repo) --
exactly one request of any kind, streaming or not, may be outstanding on a connection at a time,
matching Obsidian core's own Sync client.

Upload is buffered rather than truly incremental: file_chunk_header/binary/file_chunk_eof
frames accumulate into an UploadBatch-shaped list of FileChunk protos (exactly what the
existing, unmodified UploadFiles RPC already expects), and the batch is only handed to
UploadFiles() once every declared file has its EOF. Real constant-memory streaming (mirroring
streaming_upload_resource.py's per-chunk finalize) is a possible future optimization, out of
scope here -- UploadFiles's own gRPC-Web path already fully buffers today anyway, so this is
not a regression.

Download and history-download need no such buffering: DownloadFiles/DownloadHistoryVersion are
already async generators, reused completely unmodified -- each yielded FileChunk is translated
to a JSON control frame (header/eof) or a raw binary frame (data) as it's produced.
"""

import asyncio
import hashlib

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer
from pumice_server import wire_types
from pumice_server.ws_sync_resource import SyncWsSession, _UploadAccumulator, _file_chunk_to_frame


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class RecordingSender:
    def __init__(self):
        self.sent = []  # list of ("json", dict) | ("binary", bytes)

    def send_json(self, message: dict) -> None:
        self.sent.append(("json", message))

    def send_binary(self, data: bytes) -> None:
        self.sent.append(("binary", data))

    def json_messages(self, op=None):
        return [m for kind, m in self.sent if kind == "json" and (op is None or m["op"] == op)]

    def binary_frames(self):
        return [d for kind, d in self.sent if kind == "binary"]


def make_authenticated_session(tmp_path, repo, change_notifier=None):
    repo.create_device_token("tok", "alice", "test-device", 0)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=change_notifier)
    sender = RecordingSender()
    session = SyncWsSession(repo, servicer, sender.send_json, send_binary=sender.send_binary)
    session.handle_text_message('{"op":"init","payload":{"token":"tok","vaultId":"vault1"}}')
    assert session.authenticated
    return session, sender, servicer


def run(coro):
    if coro is not None:
        asyncio.run(coro)


def send_text(session, op, payload):
    import json
    return session.handle_text_message(json.dumps({"op": op, "payload": payload}))


# ─── _UploadAccumulator (pure) ──────────────────────────────────────────────────────────────


def test_upload_accumulator_is_not_complete_until_declared_file_count_reached():
    acc = _UploadAccumulator(vault_id="v1", file_count=2)
    assert acc.is_complete is False

    acc.add_header("a.md", 3, 1000)
    acc.add_data("a.md", 0, b"abc")
    acc.add_eof("a.md", "hash-a")
    assert acc.is_complete is False

    acc.add_header("b.md", 3, 1000)
    acc.add_eof("b.md", "hash-b")
    assert acc.is_complete is True


def test_upload_accumulator_builds_file_chunk_protos_in_order():
    acc = _UploadAccumulator(vault_id="v1", file_count=1)
    acc.add_header("a.md", 3, 1000)
    acc.add_data("a.md", 0, b"abc")
    acc.add_eof("a.md", "hash-a")

    # WhichOneof() is a deliberate compatibility shim on wire_types.FileChunk (see its
    # docstring) so this line reads identically to when acc.chunks held sync_pb2.FileChunk.
    kinds = [c.WhichOneof("payload") for c in acc.chunks]
    assert kinds == ["header", "data", "eof"]
    assert acc.chunks[1].data.data == b"abc"
    # PR5 (pumice's 13_PR5_계획.md): must be plain dataclasses now, not protobuf messages.
    assert all(isinstance(c, wire_types.FileChunk) for c in acc.chunks)


def test_upload_accumulator_add_header_passes_through_the_declared_content_hash():
    """Item 6 (2026-08 re-analysis follow-up): the header may now declare contentHash up front,
    not just at eof -- see wire_types.ChunkHeader's docstring."""
    acc = _UploadAccumulator(vault_id="v1", file_count=1)
    acc.add_header("a.md", 3, 1000, content_hash="hash-a")

    assert acc.chunks[0].header.content_hash == "hash-a"


def test_upload_accumulator_add_header_defaults_content_hash_to_empty_string():
    acc = _UploadAccumulator(vault_id="v1", file_count=1)
    acc.add_header("a.md", 3, 1000)

    assert acc.chunks[0].header.content_hash == ""


# ─── _file_chunk_to_frame (pure) ────────────────────────────────────────────────────────────


def test_file_chunk_to_frame_translates_header():
    chunk = wire_types.FileChunk(header=wire_types.ChunkHeader(vault_id="v1", path="a.md", total_bytes=3, modified_at_ms=1000))
    assert _file_chunk_to_frame(chunk) == ("json", "file_chunk_header", {"vaultId": "v1", "path": "a.md", "totalBytes": 3, "modifiedAtMs": 1000})


def test_file_chunk_to_frame_translates_data_as_raw_binary():
    chunk = wire_types.FileChunk(data=wire_types.ChunkData(path="a.md", sequence=0, data=b"abc"))
    assert _file_chunk_to_frame(chunk) == ("binary", b"abc")


def test_file_chunk_to_frame_translates_eof():
    chunk = wire_types.FileChunk(eof=wire_types.ChunkEOF(path="a.md", content_hash="deadbeef"))
    assert _file_chunk_to_frame(chunk) == ("json", "file_chunk_eof", {"path": "a.md", "contentHash": "deadbeef"})


# ─── upload end-to-end (real servicer + real vault dir) ────────────────────────────────────


def test_single_file_upload_round_trip_writes_the_file_and_acks(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"hello from ws upload"
    content_hash = hashlib.sha256(content).hexdigest()

    send_text(session, "upload_begin", {"vaultId": "vault1", "fileCount": 1})
    send_text(session, "file_chunk_header", {"vaultId": "vault1", "path": "note.md", "totalBytes": len(content), "modifiedAtMs": 1000})
    session.handle_binary_frame(content)
    coro = send_text(session, "file_chunk_eof", {"path": "note.md", "contentHash": content_hash})
    run(coro)

    [ack] = sender.json_messages("upload_ack")
    assert ack["payload"] == {"path": "note.md", "ok": True, "error": ""}

    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert written.read_bytes() == content

    # Explicit terminal signal -- the client has no other reliable way to know a streaming op is
    # fully done (counting acks/eofs against what it sent isn't enough for downloads, since the
    # server can silently skip a requested path -- see the module note).
    assert sender.json_messages("stream_end")


def test_two_file_batch_upload_sends_two_acks_and_only_finalizes_after_both(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)

    send_text(session, "upload_begin", {"vaultId": "vault1", "fileCount": 2})
    for path, content in [("a.md", b"AAA"), ("b.md", b"BBBB")]:
        send_text(session, "file_chunk_header", {"vaultId": "vault1", "path": path, "totalBytes": len(content), "modifiedAtMs": 1000})
        session.handle_binary_frame(content)
        h = hashlib.sha256(content).hexdigest()
        coro = send_text(session, "file_chunk_eof", {"path": path, "contentHash": h})
        if coro is not None:
            run(coro)

    acks = sender.json_messages("upload_ack")
    assert {a["payload"]["path"] for a in acks} == {"a.md", "b.md"}
    assert all(a["payload"]["ok"] for a in acks)
    assert session._stream_active is False  # released once the batch finished


def test_upload_hash_mismatch_is_acked_as_a_failure_not_a_crash(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"tampered content"

    send_text(session, "upload_begin", {"vaultId": "vault1", "fileCount": 1})
    send_text(session, "file_chunk_header", {"vaultId": "vault1", "path": "note.md", "totalBytes": len(content), "modifiedAtMs": 1000})
    session.handle_binary_frame(content)
    coro = send_text(session, "file_chunk_eof", {"path": "note.md", "contentHash": "0" * 64})
    run(coro)

    [ack] = sender.json_messages("upload_ack")
    assert ack["payload"]["ok"] is False


def test_upload_end_to_end_carries_the_header_content_hash_through_to_a_mismatch_rejection(tmp_path, repo):
    """Item 6 (2026-08 re-analysis follow-up): a contentHash declared in file_chunk_header that
    disagrees with the one declared at file_chunk_eof is rejected -- full WS round trip, not
    just the pure _UploadAccumulator/service.py unit tests."""
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"hello from ws upload"
    real_hash = hashlib.sha256(content).hexdigest()

    send_text(session, "upload_begin", {"vaultId": "vault1", "fileCount": 1})
    send_text(session, "file_chunk_header", {
        "vaultId": "vault1", "path": "note.md", "totalBytes": len(content), "modifiedAtMs": 1000,
        "contentHash": "declared-in-header-and-wrong",
    })
    session.handle_binary_frame(content)
    coro = send_text(session, "file_chunk_eof", {"path": "note.md", "contentHash": real_hash})
    run(coro)

    [ack] = sender.json_messages("upload_ack")
    assert ack["payload"]["ok"] is False
    assert "header" in ack["payload"]["error"].lower()


# ─── download end-to-end ─────────────────────────────────────────────────────────────────────


def test_download_req_streams_header_binary_eof_in_order(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    content = b"content to download"
    (vault_path / "note.md").write_bytes(content)

    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    coro = send_text(session, "download_req", {"vaultId": "vault1", "paths": ["note.md"]})
    run(coro)

    header = sender.json_messages("file_chunk_header")[-1]
    eof = sender.json_messages("file_chunk_eof")[-1]
    assert header["payload"]["path"] == "note.md"
    assert eof["payload"]["contentHash"] == hashlib.sha256(content).hexdigest()
    assert b"".join(sender.binary_frames()) == content
    assert session._stream_active is False
    assert sender.json_messages("stream_end")


def test_download_req_sends_stream_end_even_when_a_requested_path_is_silently_skipped(tmp_path, repo):
    # DownloadFiles just returns early for a path that doesn't exist server-side -- no header,
    # no eof, no ack. stream_end is the only signal the client has that this stream is done.
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    session, sender, servicer = make_authenticated_session(tmp_path, repo)

    run(send_text(session, "download_req", {"vaultId": "vault1", "paths": ["does-not-exist.md"]}))

    assert sender.json_messages("file_chunk_header") == []
    assert sender.json_messages("stream_end")


def test_history_dl_req_streams_a_backed_up_version(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_bytes(b"v1 content")

    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    hist_coro = send_text(session, "history_req", {"vaultId": "vault1", "path": "note.md"})
    run(hist_coro)
    history_id = sender.json_messages("history_res")[-1]["payload"]["versions"][0]["historyId"]

    dl_coro = send_text(session, "history_dl_req", {"vaultId": "vault1", "path": "note.md", "historyId": history_id})
    run(dl_coro)

    assert b"".join(sender.binary_frames()) == b"v1 content"


# ─── single active stream enforcement ───────────────────────────────────────────────────────


def test_second_streaming_op_is_rejected_while_one_is_active(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)

    send_text(session, "upload_begin", {"vaultId": "vault1", "fileCount": 1})  # left open (no eof yet)
    coro = send_text(session, "download_req", {"vaultId": "vault1", "paths": ["note.md"]})

    assert coro is None
    err = sender.json_messages("error")[-1]
    assert err["payload"]["code"] == "STREAM_IN_PROGRESS"


def test_stream_lock_is_released_after_completion_allowing_the_next_one(tmp_path, repo):
    vault_path = tmp_path / "vaults" / "alice" / "vault1"
    vault_path.mkdir(parents=True)
    (vault_path / "note.md").write_bytes(b"x")
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    servicer._scan_and_merge(str(vault_path), "alice", "vault1")

    run(send_text(session, "download_req", {"vaultId": "vault1", "paths": ["note.md"]}))
    coro = send_text(session, "download_req", {"vaultId": "vault1", "paths": ["note.md"]})

    assert coro is not None  # not rejected -- previous stream already finished
    run(coro)
    assert not sender.json_messages("error")
