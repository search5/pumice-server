"""Support for the optional plugin_autofill_paths/plugin_autofill entry points (see
plugin_registry.py) -- reading a plugin-declared, small set of vault-relative files so
plugin_loader.suggest_plugin_autofill() has something to hand a plugin's suggest_autofill(), and
cleaning up whatever that function returns before it's ever shown to a browser.

Kept separate from web.py (which is untested directly, see docs/PLUGIN_DEVELOPMENT.md's testing
section) so this logic -- the part actually worth unit testing -- doesn't need a live Pyramid
request to exercise.
"""
from __future__ import annotations

import os


def read_vault_files(vault_path: str, relative_paths: list) -> dict:
    """{relative_path: content} for each of relative_paths, resolved against vault_path. A
    missing file, a path that isn't valid UTF-8 text, or a path that would resolve outside
    vault_path (relative_paths comes from a plugin's own AUTOFILL_PATHS declaration, not
    end-user input, but is still bounds-checked the same way any other path is -- a buggy or
    malicious plugin could still declare something like "../../etc/passwd") all just map to
    None rather than raising; this is autofill data for a form, never something a caller should
    have to handle an exception for.
    """
    abs_vault_path = os.path.abspath(vault_path)
    result = {}
    for rel_path in relative_paths:
        abs_path = os.path.abspath(os.path.join(abs_vault_path, rel_path))
        if abs_path != abs_vault_path and not abs_path.startswith(abs_vault_path + os.sep):
            result[rel_path] = None
            continue
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                result[rel_path] = f.read()
        except Exception:
            result[rel_path] = None
    return result


def sanitize_autofill_suggestions(suggestions: list, config_fields: list) -> list:
    """Defense in depth on top of suggest_plugin_autofill()'s own contract: strips any
    password-typed CONFIG_FIELDS key from a suggestion's values regardless of what the plugin
    actually returned, since a plugin has no legitimate way to have derived a real secret from a
    non-secret vault file -- if one shows up anyway (a bug, or a plugin author misunderstanding
    the contract), it must never reach the browser. Also drops anything not shaped like a
    suggestion (no entry_id, or not even a dict) rather than letting a malformed one through.
    """
    password_keys = {f["key"] for f in config_fields if f.get("type") == "password"}
    cleaned = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        entry_id = suggestion.get("entry_id")
        if not entry_id:
            continue
        values = suggestion.get("values") or {}
        cleaned.append({
            "entry_id": entry_id,
            "label": suggestion.get("label") or entry_id,
            "values": {k: v for k, v in values.items() if k not in password_keys},
        })
    return cleaned
