"""Server-side 3-way merge for the two Obsidian config files every device edits independently:
`bookmarks.json` and `community-plugins.json`.

Why on the server: clients used to merge on download only, so two devices uploading at the same
time simply overwrote each other (push_req carries no base version). With `baseHash` on the push,
finalize_pushed_file() merges here -- base = the version the uploading device last synced, ours =
what it uploaded, theirs = what the server holds now -- and every client then just downloads the
merged result.

Pure (no pb2/repository imports) so it is testable on its own, like history_policy.py.
"""

import json
from typing import Any, Optional

MERGED_BASENAMES = {"bookmarks.json", "community-plugins.json"}


def is_server_merged_path(rel_path: str) -> bool:
    return rel_path.rsplit("/", 1)[-1] in MERGED_BASENAMES


_MISSING = object()


def _identity(item: Any) -> Optional[tuple]:
    """Stable identity of one bookmarks.json entry, so entries are matched across versions
    instead of compared as whole objects (a group whose children changed is still the same group)."""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "group":
        return ("group", item.get("title"))
    for key in ("path", "url", "query", "title"):
        if item.get(key) is not None:
            return (kind, key, item[key])
    return None


def _keyed(items: list) -> Optional[dict]:
    out: dict = {}
    for item in items:
        ident = _identity(item)
        if ident is None or ident in out:
            return None
        out[ident] = item
    return out


def _merge_array(base: list, ours: list, theirs: list) -> list:
    everything = base + ours + theirs
    if all(isinstance(v, str) for v in everything):
        # Sets: an entry survives unless a side removed it relative to base; additions from either
        # side are kept. Order: the server's (theirs) first, then the uploader's additions, so every
        # device converges on one order.
        def keep(v: str) -> bool:
            if v in base:
                return v in ours and v in theirs
            return v in ours or v in theirs

        result: list = []
        for v in theirs + ours:
            if keep(v) and v not in result:
                result.append(v)
        return result

    b, o, t = _keyed(base), _keyed(ours), _keyed(theirs)
    if b is None or o is None or t is None:
        return ours  # shape we can't match entries in -- the uploader's version wins
    result = []
    for ident in list(t.keys()) + [k for k in o.keys() if k not in t]:
        in_base = ident in b
        if in_base and (ident not in o or ident not in t):
            continue  # removed by one side
        merged = _merge_value(b.get(ident, _MISSING), o.get(ident, _MISSING), t.get(ident, _MISSING))
        if merged is not _MISSING:
            result.append(merged)
    return result


def _merge_object(base: dict, ours: dict, theirs: dict) -> dict:
    out: dict = {}
    for key in list(theirs.keys()) + [k for k in ours.keys() if k not in theirs]:
        merged = _merge_value(base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING))
        if merged is not _MISSING:
            out[key] = merged
    return out


def _merge_value(base: Any, ours: Any, theirs: Any) -> Any:
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    # Both sides changed this value differently.
    if isinstance(ours, dict) and isinstance(theirs, dict):
        return _merge_object(base if isinstance(base, dict) else {}, ours, theirs)
    if isinstance(ours, list) and isinstance(theirs, list):
        return _merge_array(base if isinstance(base, list) else [], ours, theirs)
    return ours  # conflicting scalar (or a removal vs an edit): the uploader's value wins


def merge_config_bytes(base: Optional[bytes], ours: bytes, theirs: bytes) -> Optional[bytes]:
    """3-way merge of three JSON documents. `base` None (never recorded / pruned) merges as if the
    base were empty, i.e. a union with no deletion semantics. Returns None when `ours`/`theirs`
    can't be parsed, so the caller keeps the uploaded bytes."""
    try:
        o = json.loads(ours.decode("utf-8"))
        t = json.loads(theirs.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    b: Any = None
    if base is not None:
        try:
            b = json.loads(base.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            b = None
    if type(b) is not type(o):
        b = [] if isinstance(o, list) else {}
    merged = _merge_value(b, o, t)
    if merged is _MISSING:
        return None
    # Same formatting Obsidian writes (JSON.stringify(x, null, 2)).
    return json.dumps(merged, indent=2, ensure_ascii=False).encode("utf-8")
