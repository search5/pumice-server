import importlib
import os
import sys

import pytest

from pumice_server.plugin_loader import PluginLoadError, load_plugin_entry_point, run_enabled_plugins
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


def _install_fake_resolver(target_dir, plugin_id, module_name, body):
    """Like _install_fake_distribution, but for run_enabled_plugins() tests -- lets each
    plugin declare its own resolve()-shaped body so a pipeline of several distinct, differently
    -behaving plugins can be installed side by side in the same sys_path_dir."""
    module_dir = os.path.join(target_dir, module_name)
    os.makedirs(module_dir)
    with open(os.path.join(module_dir, "__init__.py"), "w") as f:
        f.write(body)

    dist_info = os.path.join(target_dir, f"{module_name}-0.1.0.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {plugin_id}\nVersion: 0.1.0\nSummary: A fake plugin.\n")
    with open(os.path.join(dist_info, "entry_points.txt"), "w") as f:
        f.write(f"[{ENTRY_POINT_GROUP}]\n{plugin_id} = {module_name}:resolve\n")


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


# run_enabled_plugins -- the actual publish-time orchestration: run each enabled plugin's
# resolve() over the content in turn, tolerating a plugin that fails to load or raises while
# running (a public visitor must never get a 500 because one plugin has a bug or its own
# upstream dependency is down).

def test_run_enabled_plugins_applies_a_single_plugin(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "upper-plugin", "upper_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    return content.upper()\n",
    )

    result = run_enabled_plugins("hello", ["upper-plugin"], owner_username="alice", vault_id="v1")

    assert result == "HELLO"


def test_run_enabled_plugins_passes_owner_and_vault_through(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "context-plugin", "context_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    return f'{content}:{owner_username}:{vault_id}'\n",
    )

    result = run_enabled_plugins("note", ["context-plugin"], owner_username="alice", vault_id="v1")

    assert result == "note:alice:v1"


def test_run_enabled_plugins_chains_plugins_in_plugin_id_order(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "b-plugin", "b_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    return content + 'B'\n",
    )
    _install_fake_resolver(
        sys_path_dir, "a-plugin", "a_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    return content + 'A'\n",
    )

    # requested out of order -- run_enabled_plugins must still apply them alphabetically
    # (a-plugin, then b-plugin) so the result is deterministic regardless of caller order
    result = run_enabled_plugins("", ["b-plugin", "a-plugin"], owner_username="alice", vault_id="v1")

    assert result == "AB"


def test_run_enabled_plugins_skips_a_plugin_that_fails_to_load(sys_path_dir):
    # deliberately not installed -- load_plugin_entry_point will raise PluginLoadError
    result = run_enabled_plugins("hello", ["never-installed-plugin"], owner_username="alice", vault_id="v1")

    assert result == "hello"


def test_run_enabled_plugins_skips_a_plugin_that_raises_while_running(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "broken-plugin", "broken_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    raise RuntimeError('boom')\n",
    )

    result = run_enabled_plugins("hello", ["broken-plugin"], owner_username="alice", vault_id="v1")

    assert result == "hello"


def test_run_enabled_plugins_a_broken_plugin_does_not_block_the_next_one(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "broken-plugin", "broken_plugin_mod2",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    raise RuntimeError('boom')\n",
    )
    _install_fake_resolver(
        sys_path_dir, "working-plugin", "working_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None):\n    return content + '!'\n",
    )

    result = run_enabled_plugins("hello", ["broken-plugin", "working-plugin"], owner_username="alice", vault_id="v1")

    assert result == "hello!"


def test_run_enabled_plugins_with_no_enabled_plugins_returns_content_unchanged(sys_path_dir):
    assert run_enabled_plugins("hello", [], owner_username="alice", vault_id="v1") == "hello"
