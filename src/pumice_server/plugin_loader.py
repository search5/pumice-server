"""Dynamically imports a discovered plugin's declared entry point (see plugin_registry.py for
how the `pumice_server.plugins` group is resolved). Kept separate from plugin_registry.py
because this step has a side effect (importing the module into sys.modules) that discovery
itself should not have.

Not wired into any actual publish-rendering code path in this pass -- there's no caller of
load_plugin_entry_point yet. It exists so the "load" side of the discover/enable/load lifecycle
is in place before any real plugin (e.g. harang-immich's server counterpart) is built against
it.
"""
from __future__ import annotations

from importlib.metadata import entry_points

from .plugin_registry import ENTRY_POINT_GROUP


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
