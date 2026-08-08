"""Server-side publish-plugin infrastructure: discovering which plugins are available to
enable, via Python's standard entry-point mechanism (the `pumice_server.plugins` group,
resolved through `importlib.metadata.entry_points`).

Plugins are NOT installed at runtime -- there's no admin upload flow. A plugin is baked into
the server's Docker image at build time, as a regular dependency of pumice-server itself (e.g.
`uv add harang-immich-plugin` in this repo, then rebuild the image). That's deliberate, not a
limitation worked around elsewhere: a plugin's code runs inside the very same Python process as
the web server, so installing one is exactly as consequential as installing any other server
dependency, and goes through the same review/build/redeploy path as everything else this server
depends on -- there's no runtime "upload arbitrary code" surface at all. It also means a
plugin's own dependencies get real resolution (via uv, at build time), unlike a hand-unpacked
wheel with no dependency resolution of its own.

A plugin declares itself by adding an entry point to the `pumice_server.plugins` group in its
own pyproject.toml:

    [project.entry-points."pumice_server.plugins"]
    harang-immich = "harang_immich_plugin:resolve"

Once that package is a build-time dependency of pumice-server, discover_plugins() finds it the
same way pytest/Flask/etc. find their own plugins -- nothing pumice-server-specific about the
mechanism itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "pumice_server.plugins"


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
