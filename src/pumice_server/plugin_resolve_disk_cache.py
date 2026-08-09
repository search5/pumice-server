"""Disk-persisted cache backend for plugin_loader.run_enabled_plugins()'s cache_get/cache_set --
so a plugin's resolved output (e.g. pumice-plugin-immich's base64-inlined photos) only needs to
be (re)computed when the note's content or the plugin's config actually changes, not on every
single page view, and survives a server restart (an in-memory-only cache wouldn't).

Stored as files under data_dir, not a DB column: a cached value here is essentially a whole
rendered fragment (can run to multi-MB with inlined image data), the same category of artifact as
the raw published note content itself (also stored as files, not DB rows) -- and several of this
codebase's supported DB backends (e.g. MySQL's default TEXT type, capped around 64KB) couldn't
even hold one image-heavy entry.

Keyed by a single opaque hash (see plugin_loader._resolve_cache_key), never raw owner/vault_id/
plugin_id strings, so this module never has to worry about one of those containing a "/" or
"../" -- the key is turned directly into a filename.

No eviction: unlike the in-memory cache this replaced, there's no fixed memory budget to defend,
so a stale entry (superseded by an edited note or changed plugin config) is just an orphaned file
that sits unused rather than one that must be evicted to make room. Left for later if disk usage
from accumulated stale entries ever actually becomes a problem.
"""
from __future__ import annotations

import os
import tempfile


def make_disk_cache(data_dir: str):
    """Returns a (cache_get, cache_set) pair backed by files under
    data_dir/plugin_resolve_cache/, suitable for passing straight into
    plugin_loader.run_enabled_plugins()'s cache_get/cache_set parameters."""
    cache_dir = os.path.join(data_dir, "plugin_resolve_cache")

    def _path(key: str) -> str:
        return os.path.join(cache_dir, f"{key}.cache")

    def cache_get(key: str) -> str | None:
        try:
            with open(_path(key), "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def cache_set(key: str, value: str) -> None:
        os.makedirs(cache_dir, exist_ok=True)
        # Write to a temp file and rename into place so a concurrent reader never observes a
        # partially-written cache file.
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
            os.replace(tmp_path, _path(key))
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    return cache_get, cache_set
