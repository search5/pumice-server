"""Plain dataclasses that replace the protobuf message types (sync_pb2.*) as the shape
SyncServiceServicer's RPC methods speak internally. Part of PR5 in pumice's
13_PR5_계획.md (client repo) -- WS is now the only transport, and the WS edge
(ws_sync_resource.py) already speaks JSON, so the extra JSON<->protobuf translation step
these replace was pure overhead once gRPC-Web was retired.

Field names intentionally mirror sync_pb2's snake_case (not the JSON camelCase the WS edge
uses) so call sites elsewhere in service.py -- request.vault_id, meta["path"], etc. -- don't
need to change beyond dropping the sync_pb2 import; only construction sites do.

Filled in incrementally, one RPC at a time, alongside the service.py methods that use them --
see llm-wiki/ in this repo for the per-method conversion log.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FileMeta:
    path: str
    modified_at_ms: int
    size_bytes: int
    content_hash: str
    is_deleted: bool = False


@dataclass
class DeltaRequest:
    vault_id: str
    local_files: List[FileMeta] = field(default_factory=list)


@dataclass
class DeltaResponse:
    need_upload: List[str] = field(default_factory=list)
    need_download: List[FileMeta] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


@dataclass
class ChunkHeader:
    """Still used for DownloadFiles/DownloadHistoryVersion's outgoing FileChunk stream (see
    _file_chunk_to_frame in ws_sync_resource.py) -- the old upload side that used to construct
    these directly was removed in PR6 of #14_옵시디언싱크_정렬_구현계획.md (pumice repo), which
    replaced it with a per-file push_req/push_ack exchange that carries this same
    path/size/mtime/hash shape as plain JSON instead."""

    vault_id: str
    path: str
    total_bytes: int
    modified_at_ms: int
    content_hash: str = ""


@dataclass
class ChunkData:
    path: str
    sequence: int
    data: bytes


@dataclass
class ChunkEOF:
    path: str
    content_hash: str


@dataclass
class FileChunk:
    """Mirrors sync_pb2.FileChunk's oneof(header|data|eof) -- exactly one of the three is set
    on any given instance; service.py/ws_sync_resource.py rely on that invariant."""

    header: Optional[ChunkHeader] = None
    data: Optional[ChunkData] = None
    eof: Optional[ChunkEOF] = None

    def WhichOneof(self, _oneof_group: str) -> Optional[str]:
        """Deliberately matches protobuf's Message.WhichOneof(group_name) name/signature (the
        argument is ignored -- there's only one group here) so the call sites in service.py and
        ws_sync_resource.py that used to read `chunk.WhichOneof("payload")` against a real
        sync_pb2.FileChunk don't need to change at all, only construction sites do."""
        if self.header is not None:
            return "header"
        if self.data is not None:
            return "data"
        if self.eof is not None:
            return "eof"
        return None


@dataclass
class DownloadBatchRequest:
    vault_id: str
    paths: List[str] = field(default_factory=list)


@dataclass
class HistoryVersionDownloadRequest:
    vault_id: str
    path: str
    history_id: int


@dataclass
class HistoryRequest:
    vault_id: str
    path: str


@dataclass
class HistoryVersion:
    history_id: int
    modified_at_ms: int
    size_bytes: int
    content_hash: str
    device_name: str = "Unknown Device"
    user_name: str = "Unknown User"
    # 2026-08 WS history migration follow-up (see llm-wiki/08-*.md): the REST /api/history view
    # (now removed) always surfaced these from the DB row; GetFileHistory's original protobuf-era
    # conversion never carried them, which syncHistoryModal.ts relies on for deletion markers and
    # rename-chain display.
    deleted: bool = False
    related_path: Optional[str] = None


@dataclass
class HistoryResponse:
    versions: List[HistoryVersion] = field(default_factory=list)


@dataclass
class RestoreHistoryRequest:
    vault_id: str
    path: str
    history_id: int


@dataclass
class RestoreHistoryResponse:
    ok: bool
    error: str = ""


# ─── size / purge / usernames (2026-08 Obsidian core Sync WS fidelity follow-up -- see
# #11_websocket_동기화_프로토콜_설계.md's re-analysis) ───────────────────────────────────────


@dataclass
class SizeRequest:
    vault_id: str


@dataclass
class SizeResponse:
    # -1 means "no limit configured" -- this project has no quota system (see service.py's
    # GetSize docstring), matching the real client's own -1 sentinel when it can't reach a
    # server at all.
    vault_size_bytes: int
    total_size_bytes: int
    limit_bytes: int = -1


@dataclass
class PurgeRequest:
    vault_id: str


@dataclass
class PurgeResponse:
    ok: bool
    error: str = ""


@dataclass
class UsernamesRequest:
    vault_id: str


@dataclass
class UsernamesResponse:
    # The vault owner's username followed by every account with an accepted vault-sharing
    # invite for it -- see service.py's GetUsernames docstring and 14_vault_sharing_설계.md.
    usernames: List[str] = field(default_factory=list)
