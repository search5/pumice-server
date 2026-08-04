"""Pure in-process pub/sub for "something changed in this vault" notifications -- the payload for
an SSE /watch connection (watch_resource.py). Deliberately has no timeout/reactor dependency at
all: heartbeats and per-connection timing are the SSE resource's concern, not this registry's.
Callbacks fire on every matching notify_change() call for as long as they stay subscribed, since
one SSE connection needs to receive many notifications over its lifetime.

Single-process only: this dict lives in one Twisted reactor's memory. If pumice-server is ever
scaled to multiple instances, this needs to become a shared broker (e.g. Redis pub/sub) instead --
see #10_실시간_변경_알림_구현_계획.md 7절.
"""

from typing import Callable, Dict, List, Tuple


class ChangeNotifier:
    def __init__(self) -> None:
        self._listeners: Dict[Tuple[str, str], List[Callable[[], None]]] = {}

    def subscribe(self, owner: str, vault_id: str, on_change: Callable[[], None]) -> Callable[[], None]:
        """Registers on_change to run on every notify_change(owner, vault_id) call until the
        returned unsubscribe function is called. Safe to call unsubscribe more than once."""
        key = (owner, vault_id)
        self._listeners.setdefault(key, []).append(on_change)

        def unsubscribe() -> None:
            listeners = self._listeners.get(key)
            if listeners and on_change in listeners:
                listeners.remove(on_change)

        return unsubscribe

    def notify_change(self, owner: str, vault_id: str) -> None:
        # Copied to a list before iterating: a callback could itself subscribe/unsubscribe
        # (e.g. re-entrant cleanup), which would otherwise mutate the list being iterated.
        for on_change in list(self._listeners.get((owner, vault_id), [])):
            on_change()


def did_metadata_change(before: dict, after: dict) -> bool:
    """Whether a vault's file metadata actually differs between two snapshots (e.g.
    _scan_and_merge()'s metadata_files before vs. merged_files about to be saved). Delta gets
    called on every sync/poll cycle regardless of whether anything changed, so callers must gate
    notify_change() on this -- otherwise a client's own no-op Delta would notify its own /watch
    subscription and every other device's, every single cycle. Plain dict equality already
    compares nested values recursively in Python, so this is correct as-is; named as its own
    function purely so the intent is explicit and testable at the call site."""
    return before != after
