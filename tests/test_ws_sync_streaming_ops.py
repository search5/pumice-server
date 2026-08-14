"""TDD for the binary file-chunk streaming ops (push/download/history-download).

Design note: raw binary WS frames carry no envelope at all (matching Obsidian core's framing --
see #11_websocket_동기화_프로토콜_설계.md's "왜 JSON인가" section), so the server can only ever
have ONE streaming op (push OR download OR history-download) actively producing/consuming
binary frames on a connection at a time. SyncWsSession enforces this with a single
_stream_active flag. There is also no requestId in the protocol anywhere (see
#14_옵시디언싱크_정렬_구현계획.md in the pumice repo) -- exactly one request of any kind,
streaming or not, may be outstanding on a connection at a time, matching Obsidian core's own
Sync client.

push_req (PR6 of #14_옵시디언싱크_정렬_구현계획.md) replaced the old upload_begin/
file_chunk_header/file_chunk_eof/upload_ack quartet: the client declares one file's
path/contentHash/sizeBytes/modifiedAtMs up front, and the server either (a) already has that
exact content live somewhere else in this vault (content-hash dedup, mirroring real Obsidian
Sync's own `push` op) and hard-links/copies it onto the new path without asking for any bytes,
or (b) asks for the raw binary frame and finalizes it once received. There is no more
upload_begin batch wrapper -- one push_req is exactly one file, and the connection's existing
single-outstanding-request serialization is what used to need a batch wrapper to get multiple
files processed in order.

Download and history-download need no such per-file negotiation: DownloadFiles/
DownloadHistoryVersion are already async generators, reused completely unmodified -- each
yielded FileChunk is translated to a JSON control frame (header/eof) or a raw binary frame
(data) as it's produced.
"""

import asyncio
import hashlib

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer
from pumice_server.ws_sync_resource import SyncWsSession, _file_chunk_to_frame
from pumice_server import wire_types


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


def push(session, sender, path, content, modified_at_ms=1000, declared_hash=None):
    """Drives one full push_req round trip: sends the request, waits for the server's first
    response (push_res if it needs data, push_ack directly on a dedup hit), feeds the binary
    frame only in the push_res case, then waits for completion."""
    content_hash = declared_hash if declared_hash is not None else hashlib.sha256(content).hexdigest()
    coro = send_text(session, "push_req", {
        "vaultId": "vault1", "path": path, "contentHash": content_hash,
        "sizeBytes": len(content), "modifiedAtMs": modified_at_ms,
    })
    assert coro is not None
    start_index = len(sender.sent)

    async def drive():
        task = asyncio.ensure_future(coro)
        # _run_push's dedup lookup goes through asyncio.to_thread, which needs the event loop to
        # actually notice the executor thread finished -- a single sleep(0) isn't enough to
        # guarantee that, so poll with real (tiny) sleeps until push_res/push_ack shows up.
        new_ops = []
        for _ in range(1000):
            new_ops = [m["op"] for kind, m in sender.sent[start_index:] if kind == "json"]
            if "push_res" in new_ops or "push_ack" in new_ops:
                break
            await asyncio.sleep(0.001)
        if "push_res" in new_ops:
            session.handle_binary_frame(content)
        await task

    asyncio.run(drive())


# ─── _file_chunk_to_frame (pure, still used by download/history-download) ──────────────────


def test_file_chunk_to_frame_translates_header():
    chunk = wire_types.FileChunk(header=wire_types.ChunkHeader(vault_id="v1", path="a.md", total_bytes=3, modified_at_ms=1000))
    assert _file_chunk_to_frame(chunk) == ("json", "file_chunk_header", {"vaultId": "v1", "path": "a.md", "totalBytes": 3, "modifiedAtMs": 1000})


def test_file_chunk_to_frame_translates_data_as_raw_binary():
    chunk = wire_types.FileChunk(data=wire_types.ChunkData(path="a.md", sequence=0, data=b"abc"))
    assert _file_chunk_to_frame(chunk) == ("binary", b"abc")


def test_file_chunk_to_frame_translates_eof():
    chunk = wire_types.FileChunk(eof=wire_types.ChunkEOF(path="a.md", content_hash="deadbeef"))
    assert _file_chunk_to_frame(chunk) == ("json", "file_chunk_eof", {"path": "a.md", "contentHash": "deadbeef"})


# ─── push_req end-to-end (real servicer + real vault dir) ──────────────────────────────────


def test_push_req_with_no_existing_match_asks_for_data_then_acks(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"hello from ws push"

    push(session, sender, "note.md", content)

    [res] = sender.json_messages("push_res")
    assert res["payload"] == {"needData": True}
    [ack] = sender.json_messages("push_ack")
    assert ack["payload"] == {"ok": True, "needData": True, "error": ""}
    written = tmp_path / "vaults" / "alice" / "vault1" / "note.md"
    assert written.read_bytes() == content
    assert sender.json_messages("stream_end")
    assert session._stream_active is False


def test_push_req_hash_mismatch_is_acked_as_a_failure_not_a_crash(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"tampered content"

    push(session, sender, "note.md", content, declared_hash="0" * 64)

    [ack] = sender.json_messages("push_ack")
    assert ack["payload"]["ok"] is False
    assert "hash" in ack["payload"]["error"].lower()
    assert sender.json_messages("stream_end")
    assert session._stream_active is False


def test_two_sequential_pushes_both_succeed_since_the_connection_serializes_them(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)

    push(session, sender, "a.md", b"AAA")
    push(session, sender, "b.md", b"BBBB")

    acks = sender.json_messages("push_ack")
    assert len(acks) == 2
    assert all(a["payload"]["ok"] for a in acks)
    assert session._stream_active is False


# ─── push_req content-hash dedup (PR6) ───────────────────────────────────────────────────────


def test_push_req_skips_data_entirely_when_the_content_already_exists_live_in_this_vault(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    content = b"shared content"
    push(session, sender, "original.md", content)
    sender.sent.clear()

    content_hash = hashlib.sha256(content).hexdigest()
    coro = send_text(session, "push_req", {
        "vaultId": "vault1", "path": "copy.md", "contentHash": content_hash,
        "sizeBytes": len(content), "modifiedAtMs": 2000,
    })
    run(coro)

    # No push_res (which would mean "send me the bytes") -- push_ack comes straight back.
    assert sender.json_messages("push_res") == []
    [ack] = sender.json_messages("push_ack")
    assert ack["payload"] == {"ok": True, "needData": False, "error": ""}
    assert sender.binary_frames() == []
    copied = tmp_path / "vaults" / "alice" / "vault1" / "copy.md"
    assert copied.read_bytes() == content


def test_push_req_dedup_does_not_match_a_different_vault(tmp_path, repo):
    repo.create_device_token("tok-bob", "bob", "bobs-device", 0)
    bob_servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=repo)
    bob_sender = RecordingSender()
    bob_session = SyncWsSession(repo, bob_servicer, bob_sender.send_json, send_binary=bob_sender.send_binary)
    bob_session.handle_text_message('{"op":"init","payload":{"token":"tok-bob","vaultId":"vault1"}}')
    push(bob_session, bob_sender, "bobs-file.md", b"shared content")

    session, sender, servicer = make_authenticated_session(tmp_path, repo)
    # Drives the full round trip (push() feeds the binary frame once push_res arrives) -- a bare
    # send_text()+run() would hang forever here since _run_push awaits a binary frame that
    # nothing would ever send.
    push(session, sender, "alice-file.md", b"shared content")

    # Bob's file must not leak into alice's dedup lookup -- this had to actually ask for bytes
    # (a dedup hit would have skipped straight to push_ack with no push_res at all).
    assert sender.json_messages("push_res") == [{"op": "push_res", "payload": {"needData": True}}]


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

    # Deliberately left open (never scheduled/awaited, no binary sent) -- only the synchronous
    # claim in handle_text_message matters for this test. Closed explicitly so pytest doesn't
    # warn about an unawaited coroutine.
    open_push_coro = send_text(session, "push_req", {"vaultId": "vault1", "path": "a.md", "contentHash": "h", "sizeBytes": 1, "modifiedAtMs": 1})
    coro = send_text(session, "download_req", {"vaultId": "vault1", "paths": ["note.md"]})

    assert coro is None
    err = sender.json_messages("error")[-1]
    assert err["payload"]["code"] == "STREAM_IN_PROGRESS"
    open_push_coro.close()


def test_a_second_push_req_is_rejected_while_one_is_already_active(tmp_path, repo):
    session, sender, servicer = make_authenticated_session(tmp_path, repo)

    first = send_text(session, "push_req", {"vaultId": "vault1", "path": "a.md", "contentHash": "h", "sizeBytes": 1, "modifiedAtMs": 1})
    second = send_text(session, "push_req", {"vaultId": "vault1", "path": "b.md", "contentHash": "h2", "sizeBytes": 1, "modifiedAtMs": 1})

    assert second is None
    err = sender.json_messages("error")[-1]
    assert err["payload"]["code"] == "STREAM_IN_PROGRESS"
    first.close()


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
