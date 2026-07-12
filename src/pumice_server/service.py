import asyncio
import os
import shutil
import time
import hashlib
import grpc
import logging
import secrets
from typing import Dict, Optional
from urllib.parse import unquote

from . import sync_pb2
from . import sync_pb2_grpc
from .repository import MetadataRepository

logger = logging.getLogger(__name__)

def calculate_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def _backup_file(src_path: str, dst_path: str) -> None:
    # A hard link is a directory-entry-only operation (no data copy), so it's far cheaper than
    # shutil.copy2 for backups. It's safe here because the vault write path never edits a file
    # in place — it always writes to a temp file and os.rename()s it in, so the old inode (and
    # anything hard-linked to it) is left untouched. Falls back to an actual copy when hard
    # links aren't available (e.g. history dir on a different filesystem).
    try:
        os.link(src_path, dst_path)
    except OSError:
        shutil.copy2(src_path, dst_path)

class SyncServiceServicer(sync_pb2_grpc.SyncServiceServicer):
    def __init__(self, data_dir: str, repository: MetadataRepository):
        self.data_dir = data_dir
        self.vaults_dir = os.path.join(data_dir, "vaults")
        self.repository = repository
        os.makedirs(self.vaults_dir, exist_ok=True)

    def _verify_vault_access(self, vault_id: str, context) -> str:
        """Resolves and returns the caller's own username from their device token, aborting the
        RPC if the token is missing/invalid. A vault's identity is (owner_username, vault_id), and
        owner_username always comes from this resolved caller identity, never from client input --
        so there's nothing left to check beyond authentication itself: a caller can only ever
        address vaults under their own name. (An earlier version of this checked vault_id against
        a global vault_id -> owner registry and let the first caller "claim" an unclaimed vault_id
        -- removed, because vault_id alone isn't globally unique: "Obsidian Vault" is Obsidian's
        own default vault name, so two different accounts' same-named vaults collided.)"""
        metadata = dict(context.invocation_metadata())
        auth_header = metadata.get('authorization', '')
        if not auth_header:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing authorization metadata")
            return ""

        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
        else:
            token = auth_header.strip()

        device = self.repository.get_device_token(token)
        if not device:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid or missing auth token")
            return ""

        return device["username"]

    def _get_vault_path(self, owner_username: str, vault_id: str) -> str:
        owner_dir = os.path.join(self.vaults_dir, owner_username)
        vault_path = os.path.abspath(os.path.join(owner_dir, vault_id))
        # Security check: make sure vault_path is actually a subfolder of owner_dir
        if not vault_path.startswith(os.path.abspath(owner_dir)):
            raise ValueError("Invalid vault ID")
        return vault_path

    def _scan_and_merge(self, vault_path: str, owner_username: str, vault_id: str) -> dict:
        metadata_files = self.repository.load_all(owner_username, vault_id)
        current_files = {}

        if os.path.exists(vault_path):
            # All add_history calls from this scan share one commit instead of one fsync per
            # newly-discovered file (this matters most on the very first scan of a vault).
            with self.repository.batch() as batch:
                for root, dirs, files in os.walk(vault_path):
                    # Filter out excluded folders
                    rel_root = os.path.relpath(root, vault_path)
                    if rel_root == ".":
                        rel_root = ""

                    for d in list(dirs):
                        d_rel = os.path.normpath(os.path.join(rel_root, d)).replace("\\", "/")
                        if (d_rel == ".trash" or
                            d_rel == ".obsidian/cache" or
                            d_rel == ".obsidian/plugins/pumice" or
                            d == ".git"):
                            dirs.remove(d)

                    for file in files:
                        file_rel = os.path.normpath(os.path.join(rel_root, file)).replace("\\", "/")

                        # Filter out excluded files
                        if (file_rel == ".obsidian/workspace" or
                            file_rel == ".obsidian/workspace.json" or
                            file_rel == ".obsidian/workspace-mobile.json" or
                            file.startswith("._")):
                            continue

                        full_path = os.path.join(root, file)
                        try:
                            stat = os.stat(full_path)
                            mtime_ms = int(stat.st_mtime * 1000)
                            size = stat.st_size

                            existing = metadata_files.get(file_rel)
                            if (existing and
                                not existing.get("is_deleted") and
                                existing.get("modified_at_ms") == mtime_ms and
                                existing.get("size_bytes") == size):
                                content_hash = existing["content_hash"]
                            else:
                                content_hash = calculate_sha256(full_path)

                            current_files[file_rel] = {
                                "path": file_rel,
                                "modified_at_ms": mtime_ms,
                                "size_bytes": size,
                                "content_hash": content_hash,
                                "is_deleted": False
                            }

                            if not existing:
                                try:
                                    timestamp = int(time.time() * 1000)
                                    backup_dir = os.path.join(self.data_dir, "history", owner_username, vault_id)
                                    os.makedirs(backup_dir, exist_ok=True)
                                    backup_file_path = os.path.join(backup_dir, f"{timestamp}_{secrets.token_hex(4)}.bak")

                                    _backup_file(full_path, backup_file_path)

                                    batch.add_history(
                                        owner_username=owner_username,
                                        vault_id=vault_id,
                                        path=file_rel,
                                        modified_at_ms=mtime_ms,
                                        size_bytes=size,
                                        content_hash=content_hash,
                                        backup_file_path=backup_file_path,
                                        device_name="Server Scan",
                                        user_name="System"
                                    )
                                    logger.info(f"First Scan: Backup version created for {file_rel} -> {backup_file_path}")
                                except Exception as backup_err:
                                    logger.error(f"First Scan: Failed to create backup version for {file_rel}: {backup_err}")
                        except Exception as e:
                            logger.warning(f"Error scanning file {full_path}: {e}")

        # Merge (preserving tombstones)
        merged_files = {}
        for path, meta in current_files.items():
            merged_files[path] = meta

        for path, old_meta in metadata_files.items():
            if path not in current_files:
                if not old_meta.get("is_deleted"):
                    # Missing from the actual directory but not marked deleted in metadata -> treat as newly deleted
                    merged_files[path] = {
                        "path": path,
                        "modified_at_ms": int(time.time() * 1000),
                        "size_bytes": 0,
                        "content_hash": "",
                        "is_deleted": True
                    }
                else:
                    # Keep the existing deleted state
                    merged_files[path] = old_meta

        # Persist the metadata updates
        self.repository.save_all(owner_username, vault_id, merged_files)
        return merged_files

    def _delete_on_server(self, vault_path: str, owner_username: str, vault_id: str, path: str,
                           old_meta: Optional[dict], c_time: int, s_time: int,
                           device_name: str, user_name: str) -> dict:
        file_path = os.path.join(vault_path, path)
        if os.path.exists(file_path):
            try:
                # Back up the last physical version right before deleting it. Kept as its own
                # try/except so a backup failure (disk full, permissions, ...) never blocks the
                # tombstone below from being recorded.
                try:
                    stat = os.stat(file_path)
                    old_mtime = old_meta.get("modified_at_ms") if old_meta else int(stat.st_mtime * 1000)
                    old_size = old_meta.get("size_bytes") if old_meta else stat.st_size
                    old_hash = old_meta.get("content_hash") if old_meta else calculate_sha256(file_path)

                    timestamp = int(time.time() * 1000)
                    backup_dir = os.path.join(self.data_dir, "history", owner_username, vault_id)
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_file_path = os.path.join(backup_dir, f"{timestamp}_{secrets.token_hex(4)}.bak")

                    _backup_file(file_path, backup_file_path)

                    # Both history rows (the last version + the deletion marker) share one commit.
                    with self.repository.batch() as batch:
                        batch.add_history(
                            owner_username=owner_username,
                            vault_id=vault_id,
                            path=path,
                            modified_at_ms=old_mtime,
                            size_bytes=old_size,
                            content_hash=old_hash,
                            backup_file_path=backup_file_path,
                            device_name=device_name,
                            user_name=user_name
                        )
                        logger.info(f"Backup version created before deletion for {path} -> {backup_file_path}")

                        # Record the deletion itself as its own marker in history (same role as
                        # core Sync's "file deleted" entry)
                        batch.add_history(
                            owner_username=owner_username,
                            vault_id=vault_id,
                            path=path,
                            modified_at_ms=timestamp,
                            size_bytes=0,
                            content_hash="",
                            backup_file_path="",
                            device_name=device_name,
                            user_name=user_name,
                            deleted=True,
                        )
                except Exception as backup_err:
                    logger.error(f"Failed to create backup before deletion: {backup_err}")

                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete file {file_path} on delta: {e}")

        # Record the server metadata as deleted too
        meta = {
            "path": path,
            "modified_at_ms": max(c_time, s_time) + 1,  # ensures the sync tombstone is always the newest
            "size_bytes": 0,
            "content_hash": "",
            "is_deleted": True
        }
        self.repository.save_one(owner_username, vault_id, path, meta)
        return meta

    async def Ping(self, request, context):
        return sync_pb2.Pong(
            server_version="0.1.0",
            timestamp_ms=int(time.time() * 1000)
        )

    async def Delta(self, request, context):
        logger.info(f"gRPC Delta called: vault_id={request.vault_id}, local_files_count={len(request.local_files)}")
        try:
            owner_username = self._verify_vault_access(request.vault_id, context)
            vault_path = self._get_vault_path(owner_username, request.vault_id)
        except ValueError as e:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
            return

        # 1. Scan and merge the server's current metadata (offloaded to a worker thread —
        # this walks + hashes the whole vault and must not block the shared event loop)
        server_files = await asyncio.to_thread(self._scan_and_merge, vault_path, owner_username, request.vault_id)

        # 2. Parse the client's metadata
        client_files = {}
        for f in request.local_files:
            client_files[f.path] = {
                "path": f.path,
                "modified_at_ms": f.modified_at_ms,
                "size_bytes": f.size_bytes,
                "content_hash": f.content_hash,
                "is_deleted": f.is_deleted
            }

        need_upload = []
        need_download = []

        # Compare every file
        all_paths = set(client_files.keys()) | set(server_files.keys())

        for path in all_paths:
            client_meta = client_files.get(path)
            server_meta = server_files.get(path)

            if client_meta and not server_meta:
                # Exists only on the client
                if client_meta["is_deleted"]:
                    # Already deleted and also absent on the server -> ignore
                    continue
                # Needs upload
                need_upload.append(path)

            elif server_meta and not client_meta:
                # Exists only on the server
                if server_meta["is_deleted"]:
                    # Deleted and also absent on the client -> ignore
                    continue
                # Needs download
                need_download.append(sync_pb2.FileMeta(**server_meta))

            else:
                # Exists on both sides
                c_hash = client_meta["content_hash"]
                s_hash = server_meta["content_hash"]
                c_del = client_meta["is_deleted"]
                s_del = server_meta["is_deleted"]
                c_time = client_meta["modified_at_ms"]
                s_time = server_meta["modified_at_ms"]

                # 1. Handle the deleted (tombstone) comparison first, above all else
                if c_del and not s_del:
                    # Deleted on the client -> delete the server-side file immediately and update
                    # the tombstone. Offloaded to a worker thread (file I/O + DB writes) so this
                    # doesn't block other clients' RPCs while it runs.
                    client_metadata = dict(context.invocation_metadata())
                    device_name = unquote(client_metadata.get("x-device-name", "Unknown Device"))
                    user_name = unquote(client_metadata.get("x-user-name", "Unknown User"))
                    meta = await asyncio.to_thread(
                        self._delete_on_server, vault_path, owner_username, request.vault_id, path,
                        server_meta, c_time, s_time, device_name, user_name
                    )
                    server_files[path] = meta
                    continue

                elif s_del and not c_del:
                    # Deleted on the server -> propagate the tombstone to the client (a download
                    # instruction that prompts the client to remove its own file)
                    need_download.append(sync_pb2.FileMeta(**server_meta))
                    continue

                # 2. Regular files (both sides exist) — compare hashes
                if c_hash != s_hash:
                    # If the hashes differ, sync (upload or download) unconditionally!
                    if c_time > s_time:
                        # Client is newer -> needs upload
                        need_upload.append(path)
                    elif s_time > c_time:
                        # Server is newer -> needs download
                        need_download.append(sync_pb2.FileMeta(**server_meta))
                    else:
                        # Timestamps are exactly equal -> default to uploading the client's version
                        # (a safety pin against ending up out of sync)
                        need_upload.append(path)

        return sync_pb2.DeltaResponse(
            need_upload=need_upload,
            need_download=need_download,
            conflicts=[]
        )

    def _finalize_uploaded_file(self, owner_username: str, vault_id: str, current_rel_path: str,
                                 current_temp_path: str, current_file_path: str,
                                 total_bytes: int, modified_at_ms: int, calculated_hash: str,
                                 device_name: str, user_name: str,
                                 metadata_cache: Dict[str, dict], tombstones_by_hash: Dict[str, str]):
        # metadata_cache/tombstones_by_hash are loaded once per vault per UploadFiles batch
        # (see UploadFiles) and kept up to date here, instead of re-reading the whole vault's
        # metadata from the DB for every single file — that turned an N-file batch into an
        # O(N * vault size) scan.
        dest_dir = os.path.dirname(current_file_path)
        os.makedirs(dest_dir, exist_ok=True)

        try:
            is_new_file = not os.path.exists(current_file_path)
            should_backup = True
            rename_related_path = None

            if is_new_file:
                # Check whether some previous path (old_path) exists as a deletion tombstone
                # with the same hash (an O(1) lookup instead of scanning every file)
                old_path = tombstones_by_hash.get(calculated_hash)
                if old_path:
                    try:
                        logger.info(f"Rename detected: migrating history from {old_path} to {current_rel_path}")
                        self.repository.migrate_history_on_rename(owner_username, vault_id, old_path, current_rel_path)
                        # Carry over the previous history, but also record the rename
                        # event itself as its own history entry (related_path)
                        rename_related_path = old_path
                        tombstones_by_hash.pop(calculated_hash, None)
                        metadata_cache.pop(old_path, None)
                    except Exception as migrate_err:
                        logger.error(f"Failed to migrate history on rename: {migrate_err}")
            else:
                try:
                    old_meta = metadata_cache.get(current_rel_path)
                    old_hash = old_meta.get("content_hash") if old_meta else calculate_sha256(current_file_path)
                    if old_hash == calculated_hash:
                        history_rows = self.repository.get_history(owner_username, vault_id, current_rel_path)
                        if not history_rows:
                            should_backup = True
                            logger.info(f"First history for {current_rel_path} - force backup despite same hash.")
                        else:
                            should_backup = False
                            logger.info(f"Skip duplicate backup for {current_rel_path} (same hash: {calculated_hash})")
                except Exception as hash_err:
                    logger.error(f"Failed to check duplicate hash: {hash_err}")

            if os.path.exists(current_file_path):
                os.remove(current_file_path)
            os.rename(current_temp_path, current_file_path)

            # Set the modification time
            mtime_sec = modified_at_ms / 1000.0
            os.utime(current_file_path, (mtime_sec, mtime_sec))

            meta = {
                "path": current_rel_path,
                "modified_at_ms": modified_at_ms,
                "size_bytes": total_bytes,
                "content_hash": calculated_hash,
                "is_deleted": False
            }

            # Copy the new version into the backup directory first. Kept as its own try/except
            # (separate from the DB write below) so a filesystem backup failure never blocks the
            # metadata update that makes the upload count as successful.
            backup_file_path = None
            if should_backup:
                try:
                    timestamp = int(time.time() * 1000)
                    backup_dir = os.path.join(self.data_dir, "history", owner_username, vault_id)
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_file_path = os.path.join(backup_dir, f"{timestamp}_{secrets.token_hex(4)}.bak")
                    _backup_file(current_file_path, backup_file_path)
                except Exception as backup_err:
                    logger.error(f"Failed to create backup version for {current_rel_path}: {backup_err}")
                    backup_file_path = None

            # The metadata update and the history entry (if any) share a single commit.
            with self.repository.batch() as batch:
                batch.save_one(owner_username, vault_id, current_rel_path, meta)
                if backup_file_path:
                    batch.add_history(
                        owner_username=owner_username,
                        vault_id=vault_id,
                        path=current_rel_path,
                        modified_at_ms=modified_at_ms,
                        size_bytes=total_bytes,
                        content_hash=calculated_hash,
                        backup_file_path=backup_file_path,
                        device_name=device_name,
                        user_name=user_name,
                        related_path=rename_related_path,
                    )

            if backup_file_path:
                logger.info(f"Backup version created for {current_rel_path} -> {backup_file_path}")

            # Keep the in-memory cache in sync so later files in this same batch see this
            # file's new state without a DB round trip.
            metadata_cache[current_rel_path] = meta

            return True, ""
        except Exception as e:
            logger.error(f"UploadFiles EOF processing failed for {current_rel_path}: {e}", exc_info=True)
            if os.path.exists(current_temp_path):
                os.remove(current_temp_path)
            return False, f"File move failed: {e}"

    async def UploadFiles(self, request, context):
        logger.info("gRPC UploadFiles batch started.")
        current_file_path = None
        current_rel_path = None
        current_temp_path = None
        file_handle = None
        total_bytes = 0
        received_bytes = 0
        modified_at_ms = 0
        sha256_hash = None
        vault_id = None

        # Loading a vault's metadata is O(vault size); load it once per vault seen in this
        # batch (lazily, on that vault's first file) and reuse it for every file instead of
        # reloading the whole vault from the DB per file.
        metadata_caches: Dict[str, Dict[str, dict]] = {}
        tombstone_indexes: Dict[str, Dict[str, str]] = {}
        # Every file in a batch shares one authenticated caller, so the owner_username resolved
        # on the first header applies to the whole batch (vault_id can vary chunk to chunk, but
        # the caller's own identity can't).
        owner_username = None

        try:
            for chunk in request.chunks:
                payload_type = chunk.WhichOneof("payload")
                logger.info(f"UploadFiles received chunk: payload_type={payload_type}")

                if payload_type == "header":
                    # Clean up if a previous file was left unfinished (e.g. an abnormal termination)
                    if file_handle:
                        file_handle.close()
                        file_handle = None
                        if current_temp_path and os.path.exists(current_temp_path):
                            os.remove(current_temp_path)

                    header = chunk.header
                    vault_id = header.vault_id

                    # Check ownership
                    try:
                        owner_username = self._verify_vault_access(vault_id, context)
                    except Exception as auth_err:
                        logger.error(f"UploadFiles authorization failed: {auth_err}")
                        yield sync_pb2.UploadAck(path=header.path, ok=False, error="Authorization failed")
                        continue

                    current_rel_path = os.path.normpath(header.path).replace("\\", "/")

                    # Security check: prevent escaping into a parent directory
                    if current_rel_path.startswith("..") or os.path.isabs(current_rel_path):
                        yield sync_pb2.UploadAck(path=header.path, ok=False, error="Invalid file path")
                        continue

                    vault_path = self._get_vault_path(owner_username, vault_id)
                    current_file_path = os.path.join(vault_path, current_rel_path)

                    if vault_id not in metadata_caches:
                        files = await asyncio.to_thread(self.repository.load_all, owner_username, vault_id)
                        metadata_caches[vault_id] = files
                        tombstone_indexes[vault_id] = {
                            meta["content_hash"]: path
                            for path, meta in files.items()
                            if meta.get("is_deleted") and meta.get("content_hash")
                        }

                    # Set up the temp file path
                    temp_dir = os.path.join(self.data_dir, "tmp", owner_username, vault_id)
                    os.makedirs(temp_dir, exist_ok=True)
                    current_temp_path = os.path.join(temp_dir, f"{secrets.token_hex(8)}.tmp")

                    try:
                        file_handle = open(current_temp_path, 'wb')
                    except Exception as e:
                        yield sync_pb2.UploadAck(path=header.path, ok=False, error=f"Failed to create temp file: {e}")
                        current_file_path = None
                        continue

                    total_bytes = header.total_bytes
                    modified_at_ms = header.modified_at_ms
                    received_bytes = 0
                    sha256_hash = hashlib.sha256()

                elif payload_type == "data":
                    data_payload = chunk.data
                    if not file_handle:
                        logger.warning("UploadFiles received data chunk but file_handle is null!")
                        continue

                    # Security check: make sure the chunk's path matches the header's
                    chunk_path = os.path.normpath(data_payload.path).replace("\\", "/")
                    if chunk_path != current_rel_path:
                        file_handle.close()
                        file_handle = None
                        if os.path.exists(current_temp_path):
                            os.remove(current_temp_path)
                        yield sync_pb2.UploadAck(path=chunk_path, ok=False, error="Path mismatch in data chunk")
                        continue

                    file_handle.write(data_payload.data)
                    sha256_hash.update(data_payload.data)
                    received_bytes += len(data_payload.data)

                elif payload_type == "eof":
                    eof_payload = chunk.eof
                    if not file_handle:
                        logger.warning("UploadFiles received eof chunk but file_handle is null!")
                        continue

                    chunk_path = os.path.normpath(eof_payload.path).replace("\\", "/")
                    if chunk_path != current_rel_path:
                        file_handle.close()
                        file_handle = None
                        if os.path.exists(current_temp_path):
                            os.remove(current_temp_path)
                        yield sync_pb2.UploadAck(path=chunk_path, ok=False, error="Path mismatch in eof chunk")
                        continue

                    file_handle.close()
                    file_handle = None

                    calculated_hash = sha256_hash.hexdigest()
                    logger.info(f"UploadFiles verifying hash for {eof_payload.path}: calculated={calculated_hash}, expected={eof_payload.content_hash}")

                    # Verify the hash
                    if calculated_hash != eof_payload.content_hash:
                        logger.error(f"Hash mismatch for {eof_payload.path}: calculated={calculated_hash}, expected={eof_payload.content_hash}")
                        if os.path.exists(current_temp_path):
                            os.remove(current_temp_path)
                        yield sync_pb2.UploadAck(path=eof_payload.path, ok=False, error="Hash verification failed")
                        continue

                    logger.info(f"Hash verified successfully for {eof_payload.path}. Proceeding with file rename and metadata update.")

                    client_metadata = dict(context.invocation_metadata())
                    device_name = unquote(client_metadata.get("x-device-name", "Unknown Device"))
                    user_name = unquote(client_metadata.get("x-user-name", "Unknown User"))

                    # Rename + backup + DB writes are all blocking; offload to a worker thread so
                    # one large batch upload doesn't freeze every other concurrent RPC.
                    ok, error = await asyncio.to_thread(
                        self._finalize_uploaded_file, owner_username, vault_id, current_rel_path,
                        current_temp_path, current_file_path, total_bytes, modified_at_ms,
                        calculated_hash, device_name, user_name,
                        metadata_caches[vault_id], tombstone_indexes[vault_id]
                    )
                    yield sync_pb2.UploadAck(path=eof_payload.path, ok=ok, error=error)

                    current_file_path = None
                    current_rel_path = None
                    current_temp_path = None
        finally:
            if file_handle:
                file_handle.close()
                if current_temp_path and os.path.exists(current_temp_path):
                    os.remove(current_temp_path)

    async def DownloadFiles(self, request, context):
        CHUNK_SIZE = 256 * 1024
        write_lock = asyncio.Lock()

        async def stream_file(vault_id: str, rel_path: str) -> None:
            if rel_path.startswith("..") or os.path.isabs(rel_path):
                return
            try:
                owner_username = self._verify_vault_access(vault_id, context)
                vault_path = self._get_vault_path(owner_username, vault_id)
            except Exception as e:
                logger.error(f"DownloadFiles ownership check failed for vault {vault_id}: {e}")
                return

            file_path = os.path.join(vault_path, rel_path)
            file_meta = self.repository.load_one(owner_username, vault_id, rel_path)

            if not os.path.exists(file_path) or (file_meta and file_meta.get("is_deleted")):
                return

            try:
                stat = os.stat(file_path)
                mtime_ms = int(stat.st_mtime * 1000)
                total_bytes = stat.st_size
                if file_meta:
                    content_hash = file_meta.get("content_hash")
                else:
                    # Rare fallback path (no metadata row yet) — hashing can be slow for large
                    # files, so keep it off the event loop.
                    content_hash = await asyncio.to_thread(calculate_sha256, file_path)
            except Exception as e:
                logger.error(f"Failed to get file stat for {file_path}: {e}")
                return

            async with write_lock:
                await context.write(sync_pb2.FileChunk(header=sync_pb2.ChunkHeader(
                    vault_id=vault_id,
                    path=rel_path,
                    total_bytes=total_bytes,
                    modified_at_ms=mtime_ms,
                )))

            sequence = 0
            try:
                # Blocking file I/O is offloaded to a worker thread so reading one file doesn't
                # stall the event loop (and therefore every other concurrent download/RPC).
                f = await asyncio.to_thread(open, file_path, 'rb')
                try:
                    while True:
                        data = await asyncio.to_thread(f.read, CHUNK_SIZE)
                        if not data:
                            break
                        async with write_lock:
                            await context.write(sync_pb2.FileChunk(data=sync_pb2.ChunkData(
                                path=rel_path,
                                sequence=sequence,
                                data=data,
                            )))
                        sequence += 1
                finally:
                    f.close()
            except Exception as e:
                logger.error(f"Error reading file {file_path} for download: {e}")
                return

            async with write_lock:
                await context.write(sync_pb2.FileChunk(eof=sync_pb2.ChunkEOF(
                    path=rel_path,
                    content_hash=content_hash,
                )))

        tasks = []
        vault_id = request.vault_id
        for path in request.paths:
            rel_path = os.path.normpath(path).replace("\\", "/")
            tasks.append(asyncio.create_task(stream_file(vault_id, rel_path)))

        if tasks:
            await asyncio.gather(*tasks)

    async def GetFileHistory(self, request, context):
        logger.info(f"gRPC GetFileHistory called: vault_id={request.vault_id}, path={request.path}")
        try:
            owner_username = self._verify_vault_access(request.vault_id, context)
            history_rows = self.repository.get_history(owner_username, request.vault_id, request.path)
            logger.info(f"Found {len(history_rows)} history rows for {request.path}")
            versions = []
            for row in history_rows:
                versions.append(sync_pb2.HistoryVersion(
                    history_id=row["history_id"],
                    modified_at_ms=row["modified_at_ms"],
                    size_bytes=row["size_bytes"],
                    content_hash=row["content_hash"],
                    device_name=row.get("device_name", "Unknown Device"),
                    user_name=row.get("user_name", "Unknown User")
                ))
            return sync_pb2.HistoryResponse(versions=versions)
        except Exception as e:
            logger.error(f"GetFileHistory failed for {request.path}: {e}")
            await context.abort(grpc.StatusCode.INTERNAL, str(e))

    async def DownloadHistoryVersion(self, request, context):
        logger.info(f"gRPC DownloadHistoryVersion called: vault_id={request.vault_id}, history_id={request.history_id}")
        CHUNK_SIZE = 256 * 1024
        try:
            owner_username = self._verify_vault_access(request.vault_id, context)
            row = self.repository.get_history_by_id(owner_username, request.history_id)
            if not row:
                await context.abort(grpc.StatusCode.NOT_FOUND, "History version not found")
                return

            backup_file_path = row["backup_file_path"]
            if not os.path.exists(backup_file_path):
                await context.abort(grpc.StatusCode.NOT_FOUND, "Backup file not found on disk")
                return

            target_path = request.path if request.path else row["path"]

            # 1. Send the header
            header = sync_pb2.ChunkHeader(
                vault_id=request.vault_id,
                path=target_path,
                total_bytes=row["size_bytes"],
                modified_at_ms=row["modified_at_ms"]
            )
            yield sync_pb2.FileChunk(header=header)

            # 2. Send the data
            sequence = 0
            with open(backup_file_path, 'rb') as f:
                while True:
                    data = f.read(CHUNK_SIZE)
                    if not data:
                        break
                    chunk_data = sync_pb2.ChunkData(
                        path=target_path,
                        sequence=sequence,
                        data=data
                    )
                    yield sync_pb2.FileChunk(data=chunk_data)
                    sequence += 1

            # 3. Send the EOF
            eof = sync_pb2.ChunkEOF(
                path=target_path,
                content_hash=row["content_hash"]
            )
            yield sync_pb2.FileChunk(eof=eof)
        except Exception as e:
            logger.error(f"DownloadHistoryVersion failed: {e}")
            await context.abort(grpc.StatusCode.INTERNAL, str(e))

    async def RestoreHistoryVersion(self, request, context):
        try:
            owner_username = self._verify_vault_access(request.vault_id, context)
            # 1. Look up the history entry in the DB
            row = self.repository.get_history_by_id(owner_username, request.history_id)
            if not row:
                return sync_pb2.RestoreHistoryResponse(ok=False, error="History version not found")

            backup_file_path = row["backup_file_path"]
            if not os.path.exists(backup_file_path):
                return sync_pb2.RestoreHistoryResponse(ok=False, error="Backup file not found on disk")

            # 2. Set the restore target path
            target_path = request.path if request.path else row["path"]

            # 3. Locate the file on the server's physical storage
            vault_path = self._get_vault_path(owner_username, request.vault_id)
            dest_file_path = os.path.join(vault_path, target_path)

            # Security check: prevent escaping into a parent directory
            if not dest_file_path.startswith(os.path.abspath(vault_path)):
                return sync_pb2.RestoreHistoryResponse(ok=False, error="Invalid target path (escape attempt)")

            # 4. Create the directory and restore by overwriting
            os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)

            # Overwrite by physically copying the file
            if os.path.exists(dest_file_path):
                os.remove(dest_file_path)
            shutil.copy2(backup_file_path, dest_file_path)

            # A restore counts as a new change, so mtime has to be set to now — otherwise the client
            # won't recognize it as newer and won't download it
            current_mtime_ms = int(time.time() * 1000)
            mtime_sec = current_mtime_ms / 1000.0
            os.utime(dest_file_path, (mtime_sec, mtime_sec))

            # 5. Update the metadata DB
            meta = {
                "path": target_path,
                "modified_at_ms": current_mtime_ms,
                "size_bytes": row["size_bytes"],
                "content_hash": row["content_hash"],
                "is_deleted": False
            }
            self.repository.save_one(owner_username, request.vault_id, target_path, meta)

            # Record the restored state itself as a new change version in history (reusing the
            # existing path for the physical backup)
            try:
                client_metadata = dict(context.invocation_metadata())
                device_name = unquote(client_metadata.get("x-device-name", "Unknown Device"))
                user_name = unquote(client_metadata.get("x-user-name", "Unknown User"))

                self.repository.add_history(
                    owner_username=owner_username,
                    vault_id=request.vault_id,
                    path=target_path,
                    modified_at_ms=current_mtime_ms,
                    size_bytes=row["size_bytes"],
                    content_hash=row["content_hash"],
                    backup_file_path=backup_file_path,
                    device_name=device_name,
                    user_name=user_name
                )
                logger.info(f"Restore version history added for {target_path} referencing {backup_file_path}")
            except Exception as backup_err:
                logger.error(f"Failed to record restore version history for {target_path}: {backup_err}")

            logger.info(f"Server-side restore completed: vault={request.vault_id}, path={target_path}, history_id={request.history_id}")
            return sync_pb2.RestoreHistoryResponse(ok=True)
        except Exception as e:
            logger.error(f"RestoreHistoryVersion failed: {e}")
            return sync_pb2.RestoreHistoryResponse(ok=False, error=str(e))
