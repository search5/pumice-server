"""Server-side publish-plugin infrastructure: discovering which plugins are available to
enable, via Python's standard entry-point mechanism (the `pumice_server.plugins` group,
resolved through `importlib.metadata.entry_points`).

See ../../docs/PLUGIN_DEVELOPMENT.md for the full plugin-author guide (the contract, config,
caching behavior, testing, a checklist). The summary:

Plugins are NOT installed at runtime -- there's no admin upload flow. A plugin is baked into
the server's Docker image at build time, as a regular dependency of pumice-server itself (e.g.
`uv add pumice-plugin-immich` in this repo, then rebuild the image). That's deliberate, not a
limitation worked around elsewhere: a plugin's code runs inside the very same Python process as
the web server, so installing one is exactly as consequential as installing any other server
dependency, and goes through the same review/build/redeploy path as everything else this server
depends on -- there's no runtime "upload arbitrary code" surface at all. It also means a
plugin's own dependencies get real resolution (via uv, at build time), unlike a hand-unpacked
wheel with no dependency resolution of its own.

A plugin declares itself by adding an entry point to the `pumice_server.plugins` group in its
own pyproject.toml:

    [project.entry-points."pumice_server.plugins"]
    pumice-plugin-immich = "pumice_plugin_immich:resolve"

Once that package is a build-time dependency of pumice-server, discover_plugins() finds it the
same way pytest/Flask/etc. find their own plugins -- nothing pumice-server-specific about the
mechanism itself.

A plugin that needs its own persistent config (e.g. pumice-plugin-immich's Immich
server_url/token, entered once per vault and read back on every publish render -- see
plugin_config_crypto.py) can optionally also declare a second entry point, in the
`pumice_server.plugin_config_fields` group, pointing at a plain list describing the fields to
collect:

    [project.entry-points."pumice_server.plugin_config_fields"]
    pumice-plugin-immich = "pumice_plugin_immich:CONFIG_FIELDS"

    # in pumice_plugin_immich/__init__.py:
    CONFIG_FIELDS = [
        {"key": "server_url", "label": "Immich server URL", "type": "text"},
        {"key": "token", "label": "Immich API key", "type": "password"},
    ]

This is what lets the Publish tab render a real labeled form (text vs. password inputs) instead
of a raw JSON textarea, without pumice-server's own code knowing anything about what fields any
particular plugin actually needs -- it just renders whatever schema the plugin declares. This is
resolved lazily via plugin_loader.py's load_plugin_config_fields(), not here, for the same
reason `resolve` itself is loaded lazily rather than at discovery time: reading it means
importing the plugin's module, a side effect discover_plugins() should not have just to answer
"what's installed".

A plugin can optionally go one step further and suggest config values instead of leaving every
field blank: some of what it needs may already be sitting, unencrypted, in a vault-synced file --
e.g. pumice-plugin-immich's own server_url isn't a secret, and the harang-immich Obsidian plugin
already writes it (plus a stable per-profile id) into
`.obsidian/plugins/harang-immich/data.json`, which is just an ordinary file pumice-server already
syncs and stores. Declaring two more entry points opts into this:

    [project.entry-points."pumice_server.plugin_autofill_paths"]
    pumice-plugin-immich = "pumice_plugin_immich:AUTOFILL_PATHS"

    [project.entry-points."pumice_server.plugin_autofill"]
    pumice-plugin-immich = "pumice_plugin_immich:suggest_autofill"

    # in pumice_plugin_immich/__init__.py:
    AUTOFILL_PATHS = [".obsidian/plugins/harang-immich/data.json"]

    def suggest_autofill(vault_files: dict) -> list:
        ...  # reads vault_files[AUTOFILL_PATHS[0]], returns [{"entry_id", "label", "values"}, ...]

AUTOFILL_PATHS names which vault-relative files the plugin wants to read (nothing else --
pumice-server never hands a plugin unrestricted vault access); pumice-server reads exactly those
files (a missing/unreadable one just maps to None) and passes their contents to suggest_autofill()
as {relative_path: content}. The Publish tab's settings modal calls this when opened and offers
the suggestions as one-click fill-ins, so the vault owner doesn't have to hand-copy something
that's already sitting in their own synced vault. A `"password"`-typed CONFIG_FIELDS key is
stripped from suggested values regardless of what the plugin returns (a plugin has no business
suggesting a secret it can't legitimately have derived from a non-secret vault file) -- and
either entry point being undeclared, unloadable, or raising just means no suggestions, never a
broken settings modal.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "pumice_server.plugins"
CONFIG_FIELDS_ENTRY_POINT_GROUP = "pumice_server.plugin_config_fields"
AUTOFILL_PATHS_ENTRY_POINT_GROUP = "pumice_server.plugin_autofill_paths"
AUTOFILL_ENTRY_POINT_GROUP = "pumice_server.plugin_autofill"


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    description: str
    entry_point: str


def discover_plugins() -> list[PluginManifest]:
    """Every installed package that declares an entry point in the pumice_server.plugins
    group. Unlike the old unpack-a-wheel-into-a-directory design, there's no "installed but
    broken" state to report here -- if a distribution is installed at all, importlib.metadata
    can always read its own metadata; anything that goes wrong loading a plugin's actual entry
    point happens later, at load time (see plugin_loader.py), not at discovery time.
    """
    manifests = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        dist = ep.dist
        manifests.append(PluginManifest(
            plugin_id=ep.name,
            version=dist.version if dist else "0.0.0",
            description=(dist.metadata.get("Summary", "") or "") if dist else "",
            entry_point=ep.value,
        ))
    return manifests
