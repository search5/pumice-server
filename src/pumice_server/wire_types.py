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
from typing import List


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
