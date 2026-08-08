import importlib
import os
import sys

import pytest

from pumice_server.plugin_loader import PluginLoadError, load_plugin_entry_point
from pumice_server.plugin_registry import ENTRY_POINT_GROUP


def _install_fake_distribution(target_dir, entry_point="fakeplugin:resolve"):
    module_dir = os.path.join(target_dir, "fakeplugin")
    os.makedirs(module_dir)
    with open(os.path.join(module_dir, "__init__.py"), "w") as f:
        f.write("def resolve():\n    return 'ok'\n")

    dist_info = os.path.join(target_dir, "fake_plugin-0.1.0.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write("Metadata-Version: 2.1\nName: fake-plugin\nVersion: 0.1.0\nSummary: A fake plugin.\n")
    with open(os.path.join(dist_info, "entry_points.txt"), "w") as f:
        f.write(f"[{ENTRY_POINT_GROUP}]\nfake-plugin = {entry_point}\n")


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


def test_load_plugin_entry_point_returns_the_callable(sys_path_dir):
    _install_fake_distribution(sys_path_dir)

    resolve = load_plugin_entry_point("fake-plugin")

    assert resolve() == "ok"


def test_load_plugin_entry_point_raises_when_plugin_id_unknown(sys_path_dir):
    _install_fake_distribution(sys_path_dir)

    with pytest.raises(PluginLoadError):
        load_plugin_entry_point("no-such-plugin")


def test_load_plugin_entry_point_raises_when_target_module_missing(sys_path_dir):
    _install_fake_distribution(sys_path_dir, entry_point="no_such_module:resolve")

    with pytest.raises(PluginLoadError):
        load_plugin_entry_point("fake-plugin")
