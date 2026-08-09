"""Tests for discover_plugins. Plugins are found via importlib.metadata.entry_points, which
works by scanning sys.path for *.dist-info directories -- exactly how a real `pip`/`uv`-
installed package would be found. So rather than mocking importlib.metadata, these tests build
a minimal dist-info directory by hand and add it to sys.path, and let the real (unmocked)
discovery mechanism find it -- the same technique used to test pytest's own plugin discovery.
"""
import importlib
import os
import sys

import pytest

from pumice_server.plugin_registry import ENTRY_POINT_GROUP, discover_plugins

# discover_plugins() scans the whole environment, not just what a test adds to sys.path -- so in
# a dev venv that has a real plugin installed (e.g. pumice-plugin-immich, as an editable
# dependency), that plugin genuinely and correctly shows up here too. Captured once, before any
# test's sys_path_dir fixture runs, so tests can assert "what did *this test* add" rather than
# wrongly assuming the environment has zero plugins installed.
_AMBIENT_PLUGIN_IDS = {m.plugin_id for m in discover_plugins()}


def _install_fake_distribution(
    target_dir,
    distribution="fake_plugin",
    version="0.1.0",
    metadata_name="fake-plugin",
    module_name="fakeplugin",
    entry_point_name="fake-plugin",
    summary="A fake plugin for testing.",
    entry_point="fakeplugin:resolve",
):
    module_dir = os.path.join(target_dir, module_name)
    os.makedirs(module_dir)
    with open(os.path.join(module_dir, "__init__.py"), "w") as f:
        f.write("def resolve():\n    return 'ok'\n")

    dist_info = os.path.join(target_dir, f"{distribution}-{version}.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: {version}\nSummary: {summary}\n")
    if entry_point:
        with open(os.path.join(dist_info, "entry_points.txt"), "w") as f:
            f.write(f"[{ENTRY_POINT_GROUP}]\n{entry_point_name} = {entry_point}\n")


@pytest.fixture
def sys_path_dir(tmp_path):
    target_dir = str(tmp_path)
    before_path = list(sys.path)
    before_modules = set(sys.modules)
    sys.path.insert(0, target_dir)
    importlib.invalidate_caches()
    yield target_dir
    sys.path[:] = before_path
    for name in set(sys.modules) - before_modules:
        del sys.modules[name]
    importlib.invalidate_caches()


def test_discover_plugins_finds_an_installed_entry_point(sys_path_dir):
    _install_fake_distribution(sys_path_dir)

    manifests = discover_plugins()
    new_ids = {m.plugin_id for m in manifests} - _AMBIENT_PLUGIN_IDS

    assert new_ids == {"fake-plugin"}
    manifest = next(m for m in manifests if m.plugin_id == "fake-plugin")
    assert manifest.version == "0.1.0"
    assert manifest.description == "A fake plugin for testing."
    assert manifest.entry_point == "fakeplugin:resolve"


def test_discover_plugins_ignores_distributions_without_the_entry_point_group(sys_path_dir):
    _install_fake_distribution(sys_path_dir, entry_point=None)

    assert {m.plugin_id for m in discover_plugins()} == _AMBIENT_PLUGIN_IDS


def test_discover_plugins_finds_multiple_installed_plugins(sys_path_dir):
    _install_fake_distribution(
        sys_path_dir, distribution="plugin_a", metadata_name="plugin-a",
        module_name="plugin_a_mod", entry_point_name="plugin-a", entry_point="plugin_a_mod:resolve",
    )
    _install_fake_distribution(
        sys_path_dir, distribution="plugin_b", metadata_name="plugin-b",
        module_name="plugin_b_mod", entry_point_name="plugin-b", entry_point="plugin_b_mod:resolve",
    )

    manifests = discover_plugins()
    new_ids = {m.plugin_id for m in manifests} - _AMBIENT_PLUGIN_IDS

    assert new_ids == {"plugin-a", "plugin-b"}


def test_discover_plugins_adds_nothing_when_nothing_declares_the_group(sys_path_dir):
    assert {m.plugin_id for m in discover_plugins()} == _AMBIENT_PLUGIN_IDS
