"""Pure handler functions for the read-only MCP tools (5절 of MCP_STREAMABLE_HTTP_구현_계획.md,
plus get_recent_notes/search_notes added afterward as natural read-only extensions) --
deliberately free of the mcp SDK and of Twisted, wrapping
only `repository.py`'s existing methods, so they're directly unit-testable (see test_mcp_tools.py).
McpResource's on_call_tool dispatches tool-name -> one of these, then wraps the return dict into
the SDK's CallToolResult shape.

Every function takes `repository` first and `owner_username` (already resolved from the caller's
Bearer token by McpResource, never taken from tool arguments -- a tool argument for this would let
a caller impersonate any account) as the authorization boundary; repository.py's own methods already
scope every query to (owner_username, vault_id), so there is no cross-account leak risk here beyond
what repository.py itself already guards.
"""

import base64
import hashlib
import os
from typing import Callable, Optional

from .e2ee import DecryptionError, decrypt, derive_key, encrypt


class ToolError(Exception):
    """Client-facing tool error (not found, decryption failed, wrong owner, ...) -- McpResource
    catches this and turns it into an MCP tool error result, never a raw traceback."""


def list_vaults(repository, owner_username: str) -> dict:
    return {"vaults": repository.get_vaults_by_owner(owner_username)}


def list_files(repository, owner_username: str, vault_id: str) -> dict:
    files = repository.load_all(owner_username, vault_id)
    return {
        "files": [
            {
                "path": meta["path"],
                "modified_at_ms": meta["modified_at_ms"],
                "size_bytes": meta["size_bytes"],
                "content_hash": meta["content_hash"],
            }
            for meta in files.values()
            if not meta.get("is_deleted")
        ]
    }


def get_file_history(repository, owner_username: str, vault_id: str, path: str) -> dict:
    entries = repository.get_history(owner_username, vault_id, path)
    return {
        "history": [
            {
                "history_id": e["history_id"],
                "modified_at_ms": e["modified_at_ms"],
                "size_bytes": e["size_bytes"],
                "content_hash": e["content_hash"],
                "device_name": e["device_name"],
                "user_name": e["user_name"],
                "deleted": bool(e["deleted"]),
                "related_path": e.get("related_path"),
            }
            for e in entries
        ]
    }


def list_published_files(repository, owner_username: str, vault_id: str) -> dict:
    return {"published_files": repository.get_published_files(owner_username, vault_id)}


def _read_backup_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _decrypt_and_encode(raw: bytes, e2ee_password: Optional[str], vault_id: str) -> tuple[str, str]:
    """Shared by read_file_version and read_note: decrypts raw if e2ee_password is given, then
    decodes as UTF-8 text, falling back to base64 for binary content -- but only once a password
    was actually supplied. Binary content with no password more likely means "this vault is
    encrypted and you forgot the password" than "this is a real binary file", so that case gets a
    clear error instead of silently returning opaque base64."""
    if e2ee_password:
        key = derive_key(e2ee_password, vault_id)
        try:
            raw = decrypt(raw, key)
        except DecryptionError as e:
            raise ToolError(f"Decryption failed: {e}") from e

    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if not e2ee_password:
            raise ToolError(
                "Content is not valid UTF-8 text and no e2ee_password was provided -- if this "
                "vault has end-to-end encryption enabled, retry this tool call with the "
                "e2ee_password argument set."
            )
        return base64.b64encode(raw).decode("ascii"), "base64"


def read_file_version(
    repository,
    owner_username: str,
    history_id: int,
    e2ee_password: Optional[str] = None,
    read_bytes: Optional[Callable[[str], bytes]] = None,
) -> dict:
    entry = repository.get_history_by_id(owner_username, history_id)
    if entry is None:
        raise ToolError(f"No history entry with id {history_id} found for this account")
    if entry.get("deleted"):
        raise ToolError(f"History entry {history_id} is a deletion tombstone -- there is no content to read")

    reader = read_bytes or _read_backup_file
    raw = reader(entry["backup_file_path"])
    content, encoding = _decrypt_and_encode(raw, e2ee_password, entry["vault_id"])
    return {"path": entry["path"], "content": content, "encoding": encoding}


def read_note(
    repository,
    owner_username: str,
    vault_id: str,
    path: str,
    read_file: Callable[[str, str, str], bytes],
    e2ee_password: Optional[str] = None,
) -> dict:
    """Reads a note's current (live) content by path -- the one-call equivalent of
    get_file_history + read_file_version(latest history_id), for the common case of just wanting
    what's there right now."""
    meta = repository.load_one(owner_username, vault_id, path)
    if meta is None or meta.get("is_deleted"):
        raise ToolError(f"No note found at '{path}' in this vault")

    try:
        raw = read_file(owner_username, vault_id, path)
    except OSError as e:
        raise ToolError(f"Could not read '{path}': {e}") from e

    content, encoding = _decrypt_and_encode(raw, e2ee_password, vault_id)
    return {"path": path, "content": content, "encoding": encoding}


def get_recent_notes(repository, owner_username: str, vault_id: str, limit: int = 20) -> dict:
    files = repository.load_all(owner_username, vault_id)
    active = [meta for meta in files.values() if not meta.get("is_deleted")]
    active.sort(key=lambda meta: meta.get("modified_at_ms", 0), reverse=True)
    return {
        "files": [
            {
                "path": meta["path"],
                "modified_at_ms": meta["modified_at_ms"],
                "size_bytes": meta["size_bytes"],
                "content_hash": meta["content_hash"],
            }
            for meta in active[:limit]
        ]
    }


def resolve_vault_file_path(data_dir: str, owner_username: str, vault_id: str, relative_path: str) -> str:
    """Resolves a note's live on-disk path under data_dir/vaults/<owner>/<vault_id>/ (the tree
    service.py's _scan_and_merge treats as a vault's current content -- distinct from history's
    per-version backup files). Rejects any relative_path that would escape that directory, via a
    real path-component comparison (os.path.commonpath) rather than a string-prefix check, since
    e.g. ".../vault1" is not a prefix-safe boundary against ".../vault1x"."""
    vault_dir = os.path.abspath(os.path.join(data_dir, "vaults", owner_username, vault_id))
    full_path = os.path.abspath(os.path.join(vault_dir, relative_path))
    if os.path.commonpath([vault_dir, full_path]) != vault_dir:
        raise ToolError(f"Invalid path: '{relative_path}' escapes the vault directory")
    return full_path


def _try_decrypt_and_decode_for_scan(raw: bytes, key: Optional[bytes]) -> Optional[str]:
    """Shared by search_notes and search_by_frontmatter: best-effort decrypt+decode for a
    whole-vault scan. Unlike _decrypt_and_encode (used for a single targeted read), this returns
    None instead of raising on any failure -- a handful of unreadable/undecryptable/binary files
    is the normal case across a whole vault, not an error worth aborting the scan over."""
    if key is not None:
        try:
            raw = decrypt(raw, key)
        except DecryptionError:
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_vault_text_files(repository, owner_username: str, vault_id: str, read_file, key: Optional[bytes]):
    """Shared iteration for search_notes/search_by_frontmatter: yields (path, text) for every
    active file that can be read, (optionally) decrypted, and decoded as UTF-8 -- everything else
    is silently skipped (see _try_decrypt_and_decode_for_scan)."""
    files = repository.load_all(owner_username, vault_id)
    for path, meta in sorted(files.items()):
        if meta.get("is_deleted"):
            continue
        try:
            raw = read_file(owner_username, vault_id, path)
        except OSError:
            continue
        text = _try_decrypt_and_decode_for_scan(raw, key)
        if text is None:
            continue
        yield path, text


def search_notes(
    repository,
    owner_username: str,
    vault_id: str,
    query: str,
    read_file: Callable[[str, str, str], bytes],
    e2ee_password: Optional[str] = None,
    max_results: int = 50,
) -> dict:
    """Full-text search across a vault's current (non-deleted) files."""
    key = derive_key(e2ee_password, vault_id) if e2ee_password else None
    query_lower = query.lower()
    matches = []
    truncated = False

    for path, text in _scan_vault_text_files(repository, owner_username, vault_id, read_file, key):
        idx = text.lower().find(query_lower)
        if idx == -1:
            continue

        if len(matches) >= max_results:
            truncated = True
            break

        start = max(0, idx - 60)
        end = min(len(text), idx + len(query) + 60)
        snippet = text[start:end].replace("\n", " ")
        matches.append({"path": path, "snippet": snippet})

    return {"matches": matches, "truncated": truncated}


def _parse_frontmatter(text: str) -> dict:
    """Minimal 'key: value' YAML frontmatter parser (leading '---' ... '---' block). Doesn't
    handle nested structures or real YAML lists -- good enough for simple tag/status-style
    lookups; this server has no YAML dependency to reach for otherwise."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip().lower()] = value.strip().strip('"').strip("'").lower()
    return frontmatter


def search_by_frontmatter(
    repository,
    owner_username: str,
    vault_id: str,
    key: str,
    value: str,
    read_file: Callable[[str, str, str], bytes],
    e2ee_password: Optional[str] = None,
    max_results: int = 50,
) -> dict:
    """Finds notes whose YAML frontmatter has `key` containing `value` (substring, case
    insensitive -- e.g. key="tags", value="urgent" matches `tags: [project, urgent]`)."""
    encryption_key = derive_key(e2ee_password, vault_id) if e2ee_password else None
    key_lower = key.lower()
    value_lower = value.lower()
    matches = []
    truncated = False

    for path, text in _scan_vault_text_files(repository, owner_username, vault_id, read_file, encryption_key):
        frontmatter = _parse_frontmatter(text)
        if key_lower not in frontmatter or value_lower not in frontmatter[key_lower]:
            continue

        if len(matches) >= max_results:
            truncated = True
            break
        matches.append(path)

    return {"paths": matches, "truncated": truncated}


def _vault_looks_encrypted(
    repository, owner_username: str, vault_id: str, read_file: Callable[[str, str, str], bytes], sample_size: int
) -> bool:
    """Heuristic for write_note when no e2ee_password was given: sample a few of the vault's
    existing active files and see if any fail to decode as UTF-8 text. The server has no actual
    record of whether E2EE is on (it's a client-local setting, never transmitted -- see #5-1), so
    this is the same kind of best-effort inference read_note/read_file_version already use, just
    applied before a write instead of after a read. A vault with no existing files (or none
    readable) is treated as unencrypted -- there's nothing to infer from, and the first write
    establishes the convention."""
    files = repository.load_all(owner_username, vault_id)
    active_paths = sorted(path for path, meta in files.items() if not meta.get("is_deleted"))
    for sample_path in active_paths[:sample_size]:
        try:
            raw = read_file(owner_username, vault_id, sample_path)
        except OSError:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return True
    return False


def write_note(
    repository,
    owner_username: str,
    vault_id: str,
    path: str,
    content: str,
    read_file: Callable[[str, str, str], bytes],
    write_file: Callable[[str, str, str, bytes], dict],
    e2ee_password: Optional[str] = None,
    sample_size: int = 3,
) -> dict:
    """Creates or overwrites a note's current content. Unlike the read tools, a write can't afford
    to guess wrong and let the caller retry -- a plaintext write into an E2EE vault immediately
    syncs out and corrupts that file for every other device. So: if e2ee_password is given, it's
    trusted outright and always used (explicit signal wins, same convention as the read tools). If
    not given, existing files in the vault are sampled first (_vault_looks_encrypted) and the
    write is refused, rather than silently writing plaintext, if the vault looks encrypted.

    write_file does the actual disk write plus history backup-file creation (mirroring what a
    normal client sync does via service.py's own scan-and-backup logic) and reports back the real
    on-disk modified_at_ms/size_bytes/backup_file_path, which are then recorded via
    repository.save_one/add_history so this shows up exactly like any other synced version --
    visible to list_files/get_recent_notes and any other device's next sync."""
    plaintext_bytes = content.encode("utf-8")

    if e2ee_password:
        key = derive_key(e2ee_password, vault_id)
        raw = encrypt(plaintext_bytes, key)
        encrypted = True
    else:
        if _vault_looks_encrypted(repository, owner_username, vault_id, read_file, sample_size):
            raise ToolError(
                "This vault appears to have end-to-end encryption enabled (existing notes aren't "
                "plain text) -- retry with the e2ee_password argument set. Writing plain text here "
                "would break decryption for every other device syncing this vault."
            )
        raw = plaintext_bytes
        encrypted = False

    write_result = write_file(owner_username, vault_id, path, raw)
    content_hash = hashlib.sha256(raw).hexdigest()

    repository.save_one(
        owner_username,
        vault_id,
        path,
        {
            "path": path,
            "modified_at_ms": write_result["modified_at_ms"],
            "size_bytes": write_result["size_bytes"],
            "content_hash": content_hash,
            "is_deleted": False,
        },
    )
    repository.add_history(
        owner_username=owner_username,
        vault_id=vault_id,
        path=path,
        modified_at_ms=write_result["modified_at_ms"],
        size_bytes=write_result["size_bytes"],
        content_hash=content_hash,
        backup_file_path=write_result.get("backup_file_path", ""),
        device_name="MCP",
        user_name=owner_username,
    )

    return {"path": path, "size_bytes": write_result["size_bytes"], "encrypted": encrypted}
