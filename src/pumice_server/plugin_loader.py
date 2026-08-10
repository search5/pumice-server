"""Dynamically imports a discovered plugin's declared entry point (see plugin_registry.py for
how the `pumice_server.plugins` group is resolved), and runs the enabled subset of them over a
published note's content. Kept separate from plugin_registry.py because loading/running has
side effects (importing modules into sys.modules, executing third-party code) that discovery
itself should not have.

A plugin's entry point is a
`resolve(content: str, *, owner_username: str, vault_id: str, config_entries: dict) -> str`
callable -- a text-in, text-out transformer that resolves its own custom embed syntax into real
markdown/HTML, run on the same processed-markdown text (after wikilinks, before the
markdown-to-HTML pass) that parse_wikilinks() already operates on in web.py's publish_view.
`config_entries` is always passed (an empty dict if the plugin has no config or none is stored
for this vault) -- see plugin_config_crypto.py for how the entries a plugin declared via
CONFIG_FIELDS get collected, encrypted, and decrypted back into this dict, keyed by whatever
entry_id the vault owner gave each one (e.g. pumice-plugin-immich's per-profile Immich
credentials).
"""
from __future__ import annotations

import hashlib
import json
import logging
from importlib.metadata import entry_points

from .plugin_registry import AUTOFILL_ENTRY_POINT_GROUP, AUTOFILL_PATHS_ENTRY_POINT_GROUP, CONFIG_FIELDS_ENTRY_POINT_GROUP, ENTRY_POINT_GROUP

logger = logging.getLogger("pumice_server.plugin_loader")


def _resolve_cache_key(owner_username, vault_id, plugin_id, content, config_entries) -> str:
    """A single opaque hash identifying (owner, vault, plugin, content, config) -- not a raw
    tuple of those values, since owner_username/vault_id/plugin_id are caller-supplied strings
    and a cache backend (e.g. plugin_resolve_disk_cache.py, which turns this key straight into a
    filename) must never have to worry about one of them containing a "/" or "../"."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    config_hash = hashlib.sha256(json.dumps(config_entries, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    payload = json.dumps(
        {"owner": owner_username, "vault_id": vault_id, "plugin_id": plugin_id,
         "content_hash": content_hash, "config_hash": config_hash},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PluginLoadError(Exception):
    pass


def load_plugin_entry_point(plugin_id: str):
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == plugin_id]
    if not matches:
        raise PluginLoadError(f"No installed plugin declares entry point '{plugin_id}' in the '{ENTRY_POINT_GROUP}' group")

    entry_point = matches[0]
    try:
        return entry_point.load()
    except Exception as e:
        raise PluginLoadError(f"Plugin '{plugin_id}': failed to load entry point '{entry_point.value}': {e}") from e


def load_plugin_config_fields(plugin_id: str) -> list | None:
    """Optional per-plugin config schema (a plain list of {key, label, type} dicts), used to
    render a real labeled settings form -- not a raw JSON textarea -- for whatever
    server_url/token-shaped credentials a plugin needs. Returns None if the plugin
    doesn't declare this entry point (most won't) or if loading it fails for any reason; unlike
    load_plugin_entry_point(), this never raises -- a broken/missing config schema should just
    mean "no settings form for this plugin", not break the page that's trying to list plugins.
    """
    matches = [ep for ep in entry_points(group=CONFIG_FIELDS_ENTRY_POINT_GROUP) if ep.name == plugin_id]
    if not matches:
        return None

    entry_point = matches[0]
    try:
        return entry_point.load()
    except Exception as e:
        logger.error(f"Plugin '{plugin_id}' declares config fields but they failed to load: {e}")
        return None


def load_plugin_autofill_paths(plugin_id: str) -> list:
    """Optional list of vault-relative file paths a plugin wants read for autofill suggestions
    (see suggest_plugin_autofill below). Returns [] if the plugin doesn't declare this (most
    won't) or if loading it fails for any reason -- like load_plugin_config_fields(), a broken or
    missing declaration just means no suggestions, never an error surfaced anywhere.
    """
    matches = [ep for ep in entry_points(group=AUTOFILL_PATHS_ENTRY_POINT_GROUP) if ep.name == plugin_id]
    if not matches:
        return []

    entry_point = matches[0]
    try:
        return entry_point.load()
    except Exception as e:
        logger.error(f"Plugin '{plugin_id}' declares autofill paths but they failed to load: {e}")
        return []


def suggest_plugin_autofill(plugin_id: str, vault_files: dict) -> list:
    """Best-effort: loads the plugin's declared suggest_autofill(vault_files) -> list function
    (see load_plugin_autofill_paths above for how vault_files gets populated) and calls it.
    Returns [] if the plugin doesn't declare this, if loading it fails, if it raises, or if it
    returns something other than a list -- autofill is a pure UX nicety layered on top of manual
    config entry, so nothing about it is allowed to break the settings modal that calls this.
    """
    matches = [ep for ep in entry_points(group=AUTOFILL_ENTRY_POINT_GROUP) if ep.name == plugin_id]
    if not matches:
        return []

    entry_point = matches[0]
    try:
        fn = entry_point.load()
        result = fn(vault_files)
    except Exception as e:
        logger.error(f"Plugin '{plugin_id}' autofill suggestion failed: {e}")
        return []
    return result if isinstance(result, list) else []


def run_enabled_plugins(
    content: str, plugin_ids, *, owner_username: str, vault_id: str, plugin_configs: dict | None = None,
    cache_get=None, cache_set=None,
) -> str:
    """Applies each of plugin_ids' resolve() in turn (alphabetically by plugin_id, so the
    result is deterministic regardless of the order the caller happens to list them in), each
    one seeing the previous one's output. A plugin that fails to load or raises while running is
    logged and skipped, leaving content unchanged for that plugin -- publishing a page must
    never 500 because one plugin has a bug or an external dependency it calls out to is down;
    the visitor hitting a public /publish/... URL has no way to act on that error anyway.

    plugin_configs is {plugin_id: {entry_id: decrypted_config_dict}} -- the caller (web.py) is
    responsible for decrypting before calling this, since plugin_config_crypto.py needs
    PLUGIN_CONFIG_ENCRYPTION_KEY, which this module has no business knowing about.

    cache_get(key) -> str | None and cache_set(key, value) are optional; this module has no
    storage opinion of its own about where a resolved result should live (see
    plugin_resolve_disk_cache.py for the disk-backed implementation web.py actually wires in) --
    without them, every plugin is simply re-run on every call. When given, a plugin isn't
    actually re-run at all when the content it would see and its config are both identical to a
    previous call. A broken cache (a read or write that raises) is treated the same as a plugin
    failure -- logged and ignored, never allowed to break the render.
    """
    plugin_configs = plugin_configs or {}
    for plugin_id in sorted(plugin_ids):
        try:
            resolve = load_plugin_entry_point(plugin_id)
        except PluginLoadError as e:
            logger.error(f"Plugin '{plugin_id}' could not be loaded, skipping: {e}")
            continue

        config_entries = plugin_configs.get(plugin_id, {})
        cache_key = _resolve_cache_key(owner_username, vault_id, plugin_id, content, config_entries)

        cached = None
        if cache_get is not None:
            try:
                cached = cache_get(cache_key)
            except Exception as e:
                logger.error(f"Plugin '{plugin_id}': resolve cache read failed, falling back to a live resolve: {e}")
        if cached is not None:
            content = cached
            continue

        try:
            result = resolve(content, owner_username=owner_username, vault_id=vault_id, config_entries=config_entries)
        except Exception as e:
            logger.error(f"Plugin '{plugin_id}' raised while resolving published content, skipping: {e}")
            continue

        # Only cache when the plugin actually changed something. A plugin like
        # pumice-plugin-immich that catches its own failures (e.g. its Immich server being
        # briefly unreachable) and falls back to returning content unchanged looks, from here,
        # identical to "nothing to do here" -- caching that as if it were a real result would
        # permanently bake the failure in until the note or config changes again, even after
        # whatever it depends on recovers. Skipping the write means it's simply retried live next
        # time, which is cheap (that's exactly the case where resolve() had no real work to do).
        if cache_set is not None and result != content:
            try:
                cache_set(cache_key, result)
            except Exception as e:
                logger.error(f"Plugin '{plugin_id}': resolve cache write failed, ignoring: {e}")
        content = result

    return content
