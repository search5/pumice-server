"""Dynamically imports a discovered plugin's declared entry point (see plugin_registry.py for
how the `pumice_server.plugins` group is resolved), and runs the enabled subset of them over a
published note's content. Kept separate from plugin_registry.py because loading/running has
side effects (importing modules into sys.modules, executing third-party code) that discovery
itself should not have.

A plugin's entry point is a `resolve(content: str, *, owner_username: str, vault_id: str) ->
str` callable -- a text-in, text-out transformer that resolves its own custom embed syntax into
real markdown/HTML, run on the same processed-markdown text (after wikilinks, before the
markdown-to-HTML pass) that parse_wikilinks() already operates on in web.py's publish_view.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .plugin_registry import ENTRY_POINT_GROUP

logger = logging.getLogger("pumice_server.plugin_loader")


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


def run_enabled_plugins(content: str, plugin_ids, *, owner_username: str, vault_id: str) -> str:
    """Applies each of plugin_ids' resolve() in turn (alphabetically by plugin_id, so the
    result is deterministic regardless of the order the caller happens to list them in), each
    one seeing the previous one's output. A plugin that fails to load or raises while running is
    logged and skipped, leaving content unchanged for that plugin -- publishing a page must
    never 500 because one plugin has a bug or an external dependency it calls out to is down;
    the visitor hitting a public /publish/... URL has no way to act on that error anyway.
    """
    for plugin_id in sorted(plugin_ids):
        try:
            resolve = load_plugin_entry_point(plugin_id)
        except PluginLoadError as e:
            logger.error(f"Plugin '{plugin_id}' could not be loaded, skipping: {e}")
            continue

        try:
            content = resolve(content, owner_username=owner_username, vault_id=vault_id)
        except Exception as e:
            logger.error(f"Plugin '{plugin_id}' raised while resolving published content, skipping: {e}")

    return content
