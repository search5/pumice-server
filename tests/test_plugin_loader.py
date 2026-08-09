import importlib
import os
import sys

import pytest

from pumice_server.plugin_loader import PluginLoadError, load_plugin_config_fields, load_plugin_entry_point, run_enabled_plugins
from pumice_server.plugin_registry import CONFIG_FIELDS_ENTRY_POINT_GROUP, ENTRY_POINT_GROUP


def _dict_cache():
    """A trivial in-memory (owner, vault_id, plugin_id, content, config)-agnostic cache backend
    -- run_enabled_plugins() doesn't know or care whether cache_get/cache_set are backed by a
    plain dict (as in these tests) or by disk (see plugin_resolve_disk_cache.py, wired in by
    web.py for real published-page rendering)."""
    store = {}
    return store.get, store.__setitem__


def _install_counting_resolver(target_dir, plugin_id, module_name):
    """A fake resolver that counts how many times it actually ran, so cache-hit tests can
    assert the underlying plugin was (or wasn't) re-invoked."""
    _install_fake_resolver(
        target_dir, plugin_id, module_name,
        "call_count = 0\n\n"
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n"
        "    global call_count\n"
        "    call_count += 1\n"
        "    return content + '!'\n",
    )


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


def _install_fake_plugin_with_config_fields(target_dir, plugin_id="fake-plugin", module_name="fakeplugin_cfg", declare_entry_point=True):
    module_dir = os.path.join(target_dir, module_name)
    os.makedirs(module_dir)
    with open(os.path.join(module_dir, "__init__.py"), "w") as f:
        f.write(
            "def resolve(content, **kwargs):\n    return content\n\n"
            "CONFIG_FIELDS = [\n"
            "    {'key': 'server_url', 'label': 'Server URL', 'type': 'text'},\n"
            "    {'key': 'password', 'label': 'Password', 'type': 'password'},\n"
            "]\n"
        )

    dist_info = os.path.join(target_dir, f"{module_name}-0.1.0.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {plugin_id}\nVersion: 0.1.0\nSummary: A fake plugin.\n")
    entry_points_txt = f"[{ENTRY_POINT_GROUP}]\n{plugin_id} = {module_name}:resolve\n"
    if declare_entry_point:
        entry_points_txt += f"\n[{CONFIG_FIELDS_ENTRY_POINT_GROUP}]\n{plugin_id} = {module_name}:CONFIG_FIELDS\n"
    with open(os.path.join(dist_info, "entry_points.txt"), "w") as f:
        f.write(entry_points_txt)


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
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    return content.upper()\n",
    )

    result = run_enabled_plugins("hello", ["upper-plugin"], owner_username="alice", vault_id="v1")

    assert result == "HELLO"


def test_run_enabled_plugins_passes_owner_and_vault_through(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "context-plugin", "context_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    return f'{content}:{owner_username}:{vault_id}'\n",
    )

    result = run_enabled_plugins("note", ["context-plugin"], owner_username="alice", vault_id="v1")

    assert result == "note:alice:v1"


def test_run_enabled_plugins_chains_plugins_in_plugin_id_order(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "b-plugin", "b_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    return content + 'B'\n",
    )
    _install_fake_resolver(
        sys_path_dir, "a-plugin", "a_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    return content + 'A'\n",
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
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    raise RuntimeError('boom')\n",
    )

    result = run_enabled_plugins("hello", ["broken-plugin"], owner_username="alice", vault_id="v1")

    assert result == "hello"


def test_run_enabled_plugins_a_broken_plugin_does_not_block_the_next_one(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "broken-plugin", "broken_plugin_mod2",
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    raise RuntimeError('boom')\n",
    )
    _install_fake_resolver(
        sys_path_dir, "working-plugin", "working_plugin_mod",
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n    return content + '!'\n",
    )

    result = run_enabled_plugins("hello", ["broken-plugin", "working-plugin"], owner_username="alice", vault_id="v1")

    assert result == "hello!"


def test_run_enabled_plugins_with_no_enabled_plugins_returns_content_unchanged(sys_path_dir):
    assert run_enabled_plugins("hello", [], owner_username="alice", vault_id="v1") == "hello"


# Resolve caching -- a published page is re-rendered from scratch on every single view (no HTML
# caching in web.py), so without this, a plugin that calls out to an external service (e.g.
# pumice-plugin-immich fetching a photo from Immich) would redo that call for every visitor, even
# though the note's content and the plugin's config haven't changed since the last render.
# run_enabled_plugins() itself holds no cache state or storage opinion of its own -- the caller
# injects cache_get/cache_set (web.py wires up a disk-backed cache; see
# plugin_resolve_disk_cache.py -- these tests use a plain dict instead). Cached purely on (owner,
# vault, plugin_id, content, config), so a note edit or a config change is automatically a cache
# miss, with no explicit invalidation code needed anywhere.

def test_run_enabled_plugins_without_a_cache_runs_the_plugin_every_time(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod0")

    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1")
    result = run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1")

    mod = importlib.import_module("counting_plugin_mod0")
    assert result == "hello!"
    assert mod.call_count == 2  # no cache_get/cache_set given -- nothing to skip a re-run with


def test_run_enabled_plugins_caches_a_result_for_identical_content_and_config(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod")
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    result = run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("counting_plugin_mod")
    assert result == "hello!"
    assert mod.call_count == 1


def test_run_enabled_plugins_cache_misses_when_content_changes(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod2")
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    run_enabled_plugins("goodbye", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("counting_plugin_mod2")
    assert mod.call_count == 2


def test_run_enabled_plugins_cache_misses_when_config_changes(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod3")
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins(
        "hello", ["counting-plugin"], owner_username="alice", vault_id="v1",
        plugin_configs={"counting-plugin": {"e1": {"token": "a"}}}, cache_get=cache_get, cache_set=cache_set,
    )
    run_enabled_plugins(
        "hello", ["counting-plugin"], owner_username="alice", vault_id="v1",
        plugin_configs={"counting-plugin": {"e1": {"token": "b"}}}, cache_get=cache_get, cache_set=cache_set,
    )

    mod = importlib.import_module("counting_plugin_mod3")
    assert mod.call_count == 2


def test_run_enabled_plugins_cache_misses_across_different_vaults(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod4")
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v2", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("counting_plugin_mod4")
    assert mod.call_count == 2


def test_run_enabled_plugins_cache_misses_across_different_owners(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod5")
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["counting-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    run_enabled_plugins("hello", ["counting-plugin"], owner_username="bob", vault_id="v1", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("counting_plugin_mod5")
    assert mod.call_count == 2


def test_run_enabled_plugins_does_not_cache_a_result_the_plugin_left_unchanged(sys_path_dir):
    # Simulates a plugin (like pumice-plugin-immich) that swallows its own failures internally
    # and just returns content unchanged rather than raising -- e.g. a transient outage of
    # whatever external service it calls out to. If that got cached like a real result, the
    # failure would be permanently "baked in" until the note or config changes again, even after
    # the external service recovers. Skipping the cache write whenever the plugin's output is
    # identical to its input means a call like this is always retried live -- self-healing.
    _install_fake_resolver(
        sys_path_dir, "noop-on-failure-plugin", "noop_plugin_mod",
        "call_count = 0\n\n"
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n"
        "    global call_count\n"
        "    call_count += 1\n"
        "    return content\n",
    )
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["noop-on-failure-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    run_enabled_plugins("hello", ["noop-on-failure-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("noop_plugin_mod")
    assert mod.call_count == 2


def test_run_enabled_plugins_does_not_cache_a_plugin_that_raised(sys_path_dir):
    _install_fake_resolver(
        sys_path_dir, "broken-plugin", "broken_plugin_mod3",
        "call_count = 0\n\n"
        "def resolve(content, *, owner_username=None, vault_id=None, config_entries=None):\n"
        "    global call_count\n"
        "    call_count += 1\n"
        "    raise RuntimeError('boom')\n",
    )
    cache_get, cache_set = _dict_cache()

    run_enabled_plugins("hello", ["broken-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)
    run_enabled_plugins("hello", ["broken-plugin"], owner_username="alice", vault_id="v1", cache_get=cache_get, cache_set=cache_set)

    mod = importlib.import_module("broken_plugin_mod3")
    assert mod.call_count == 2


def test_run_enabled_plugins_a_cache_read_failure_falls_back_to_a_live_resolve(sys_path_dir):
    # A broken/unavailable cache backend (e.g. a disk read error) must never break rendering --
    # it should just behave as a miss.
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod8")

    def broken_cache_get(key):
        raise OSError("disk on fire")

    result = run_enabled_plugins(
        "hello", ["counting-plugin"], owner_username="alice", vault_id="v1",
        cache_get=broken_cache_get, cache_set=lambda k, v: None,
    )

    assert result == "hello!"


def test_run_enabled_plugins_a_cache_write_failure_does_not_break_the_result(sys_path_dir):
    _install_counting_resolver(sys_path_dir, "counting-plugin", "counting_plugin_mod9")

    def broken_cache_set(key, value):
        raise OSError("disk full")

    result = run_enabled_plugins(
        "hello", ["counting-plugin"], owner_username="alice", vault_id="v1",
        cache_get=lambda k: None, cache_set=broken_cache_set,
    )

    assert result == "hello!"


# load_plugin_config_fields -- the optional, lazily-loaded config schema a plugin can declare
# so the Publish tab can render a real labeled settings form instead of a raw JSON textarea.

def test_load_plugin_config_fields_returns_the_declared_schema(sys_path_dir):
    _install_fake_plugin_with_config_fields(sys_path_dir)

    fields = load_plugin_config_fields("fake-plugin")

    assert fields == [
        {"key": "server_url", "label": "Server URL", "type": "text"},
        {"key": "password", "label": "Password", "type": "password"},
    ]


def test_load_plugin_config_fields_returns_none_when_not_declared(sys_path_dir):
    _install_fake_plugin_with_config_fields(sys_path_dir, declare_entry_point=False)

    assert load_plugin_config_fields("fake-plugin") is None


def test_load_plugin_config_fields_returns_none_for_unknown_plugin(sys_path_dir):
    assert load_plugin_config_fields("no-such-plugin") is None


def test_load_plugin_config_fields_returns_none_when_target_is_broken(sys_path_dir):
    module_dir = os.path.join(sys_path_dir, "broken_cfg_mod")
    os.makedirs(module_dir)
    with open(os.path.join(module_dir, "__init__.py"), "w") as f:
        f.write("def resolve(content, **kwargs):\n    return content\n")  # no CONFIG_FIELDS defined

    dist_info = os.path.join(sys_path_dir, "broken_cfg_mod-0.1.0.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write("Metadata-Version: 2.1\nName: broken-plugin\nVersion: 0.1.0\nSummary: x\n")
    with open(os.path.join(dist_info, "entry_points.txt"), "w") as f:
        f.write(
            f"[{ENTRY_POINT_GROUP}]\nbroken-plugin = broken_cfg_mod:resolve\n\n"
            f"[{CONFIG_FIELDS_ENTRY_POINT_GROUP}]\nbroken-plugin = broken_cfg_mod:CONFIG_FIELDS\n"
        )

    assert load_plugin_config_fields("broken-plugin") is None
