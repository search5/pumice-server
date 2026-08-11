"""TDD for DownloadFiles resilience under a large batch.

Regression scenario this guards against: a fresh-vault initial sync needing ~2500 files
sent one DownloadFiles request for the entire need-download list. The server fanned out
one asyncio task per requested path with no concurrency cap, and a single file's
context.write() failure (header/eof writes were unguarded) propagated out of
asyncio.gather() and killed the *entire* RPC. The client's retry loop only shrinks its
retry list on a per-file ack -- a whole-stream failure leaves it unchanged -- so every
retry re-requested the identical full list and failed identically, forever.

Two properties are tested against the real DownloadFiles/stream_file code path (no mocking
of the method under test itself):

1. One file's write failure must not prevent the other files in the same batch from being
   delivered, and must not raise out of DownloadFiles at all.
2. Concurrent in-flight files (header sent, eof not yet sent) must never exceed the
   servicer's configured download_concurrency.
"""

import asyncio
import os

import pytest

from pumice_server import sync_pb2
from pumice_server.service import SyncServiceServicer


class FakeRepository:
    """Just enough of MetadataRepository for DownloadFiles's read path."""

    def __init__(self, username="alice", token="tok"):
        self._username = username
        self._token = token

    def get_device_token(self, token):
        if token == self._token:
            return {"username": self._username}
        return None

    def load_one(self, owner_username, vault_id, path):
        return None  # no metadata row -- exercises the calculate_sha256 fallback


class FakeContext:
    """Black-box double for NativeServicerContext. Tracks, per write() call, which files
    currently have a header written but no eof yet (max_concurrent) -- this is observable
    without touching production code, and directly reflects how many files are being
    streamed at once regardless of internal locking details.
    """

    def __init__(self, token="tok", fail_paths=None):
        self._token = token
        self.written = []
        self.active = set()
        self.max_concurrent = 0
        self._fail_paths = fail_paths or set()

    def invocation_metadata(self):
        return [("authorization", f"Bearer {self._token}")]

    def abort(self, code, details):
        raise RuntimeError(f"aborted: {details}")

    async def write(self, message):
        if message.HasField("header"):
            path = message.header.path
            if path in self._fail_paths:
                raise RuntimeError(f"simulated write failure for {path}")
            self.active.add(path)
            self.max_concurrent = max(self.max_concurrent, len(self.active))
        elif message.HasField("eof"):
            self.active.discard(message.eof.path)
        self.written.append(message)


def make_vault(tmp_path, owner="alice", vault_id="vault1", files=None):
    vault_dir = tmp_path / "vaults" / owner / vault_id
    vault_dir.mkdir(parents=True)
    for path, content in (files or {}).items():
        full = vault_dir / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
    return tmp_path


def paths_with_headers(context):
    return {m.header.path for m in context.written if m.HasField("header")}


def paths_with_eof(context):
    return {m.eof.path for m in context.written if m.HasField("eof")}


def test_one_failing_file_does_not_abort_the_whole_batch(tmp_path):
    files = {"a.md": b"aaa", "b.md": b"bbb", "c.md": b"ccc"}
    make_vault(tmp_path, files=files)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=FakeRepository())
    context = FakeContext(fail_paths={"b.md"})
    request = sync_pb2.DownloadBatchRequest(vault_id="vault1", paths=list(files.keys()))

    # Must complete without raising -- previously the header-write RuntimeError for "b.md"
    # propagated out of asyncio.gather() and killed the whole DownloadFiles call.
    asyncio.run(servicer.DownloadFiles(request, context))

    assert paths_with_eof(context) == {"a.md", "c.md"}
    assert "b.md" not in paths_with_eof(context)


def test_concurrency_is_fully_serialized_when_limit_is_one(tmp_path):
    files = {f"n{i}.md": f"content-{i}".encode() for i in range(12)}
    make_vault(tmp_path, files=files)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=FakeRepository(), download_concurrency=1)
    context = FakeContext()
    request = sync_pb2.DownloadBatchRequest(vault_id="vault1", paths=list(files.keys()))

    asyncio.run(servicer.DownloadFiles(request, context))

    assert context.max_concurrent == 1
    assert paths_with_eof(context) == set(files.keys())


def test_concurrency_never_exceeds_the_configured_limit(tmp_path):
    files = {f"n{i}.md": f"content-{i}".encode() for i in range(20)}
    make_vault(tmp_path, files=files)
    servicer = SyncServiceServicer(data_dir=str(tmp_path), repository=FakeRepository(), download_concurrency=4)
    context = FakeContext()
    request = sync_pb2.DownloadBatchRequest(vault_id="vault1", paths=list(files.keys()))

    asyncio.run(servicer.DownloadFiles(request, context))

    assert context.max_concurrent <= 4
    assert paths_with_eof(context) == set(files.keys())
