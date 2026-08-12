"""Pure in-process pub/sub for "something changed in this vault" notifications -- the source of
the WS sync endpoint's server-initiated `push` op (ws_sync_resource.py). Deliberately has no
timeout/reactor dependency at all: heartbeats and per-connection timing are the WS resource's
concern, not this registry's. Callbacks fire on every matching notify_change() call for as long
as they stay subscribed, since one connection needs to receive many notifications over its
lifetime.

Single-process only: this dict lives in one Twisted reactor's memory. If pumice-server is ever
scaled to multiple instances, this needs to become a shared broker (e.g. Redis pub/sub) instead --
see #10_실시간_변경_알림_구현_계획.md 7절.

2026-08 Obsidian core fidelity follow-up (see #11_websocket_동기화_프로토콜_설계.md and
llm-wiki/07-push-file-metadata.md): notify_change() now carries the changed file's own metadata
dict (path/modified_at_ms/size_bytes/content_hash/is_deleted) instead of a bare "something
changed" signal, matching real Obsidian Sync's push op -- callers with a batch of changed files
(see service.py's _scan_and_merge) call notify_change() once per changed file via
diff_changed_files() below, rather than once per batch.
"""

from typing import Callable, Dict, List, Tuple


class ChangeNotifier:
    def __init__(self) -> None:
        self._listeners: Dict[Tuple[str, str], List[Callable[[dict], None]]] = {}

    def subscribe(self, owner: str, vault_id: str, on_change: Callable[[dict], None]) -> Callable[[], None]:
        """Registers on_change to run on every notify_change(owner, vault_id, file_meta) call
        (receiving that file_meta) until the returned unsubscribe function is called. Safe to
        call unsubscribe more than once."""
        key = (owner, vault_id)
        self._listeners.setdefault(key, []).append(on_change)

        def unsubscribe() -> None:
            listeners = self._listeners.get(key)
            if listeners and on_change in listeners:
                listeners.remove(on_change)

        return unsubscribe

    def notify_change(self, owner: str, vault_id: str, file_meta: dict) -> None:
        # Copied to a list before iterating: a callback could itself subscribe/unsubscribe
        # (e.g. re-entrant cleanup), which would otherwise mutate the list being iterated.
        for on_change in list(self._listeners.get((owner, vault_id), [])):
            on_change(file_meta)


def _normalize_metadata(files: dict) -> dict:
    # Compares only the fields that make a file meaningfully "changed" for notification purposes
    # -- deliberately ignores everything else so mismatched dict shapes between callers can never
    # cause a false positive again (see the regression test this was added for).
    return {path: (meta.get("content_hash"), bool(meta.get("is_deleted"))) for path, meta in files.items()}


def diff_changed_files(before: dict, after: dict) -> dict:
    """The subset of `after` whose (content_hash, is_deleted) actually differs from `before`
    (including paths present only in `after`) -- one entry per file a caller with a batch of
    changes (e.g. _scan_and_merge's whole-vault rescan) should call notify_change() for
    individually, so a push carries that one file's real metadata instead of a bare "something
    changed in this vault" flag. Same normalization as did_metadata_change (ignores everything
    but content_hash/is_deleted, so DB-row-vs-scan-dict key-set mismatches between callers can
    never cause a false positive -- see that function's docstring for the incident that caused)."""
    before_norm = _normalize_metadata(before)
    after_norm = _normalize_metadata(after)
    return {path: meta for path, meta in after.items() if after_norm[path] != before_norm.get(path)}


def did_metadata_change(before: dict, after: dict) -> bool:
    """Whether a vault's file metadata actually differs between two snapshots (e.g.
    _scan_and_merge()'s metadata_files before vs. merged_files about to be saved). Delta gets
    called on every sync/poll cycle regardless of whether anything changed, so callers must gate
    notify_change() on this -- otherwise a client's own no-op Delta would notify its own /watch
    subscription and every other device's, every single cycle.

    Normalizes both sides to (content_hash, is_deleted) per path before comparing -- a plain
    `before != after` looks correct but is NOT: repository.load_all() returns full DB rows
    (owner_username, vault_id, path, ...) while _scan_and_merge() builds its own dicts with only
    5 keys (no owner_username/vault_id). That key-set mismatch made this return True on every
    single call regardless of real changes, which meant every sync -- including one triggered
    purely by receiving a live-update notification -- renotified itself: an infinite
    notify -> resync -> notify loop with no actual file change driving it."""
    return _normalize_metadata(before) != _normalize_metadata(after)
