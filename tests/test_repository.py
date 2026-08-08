"""TDD for the plugin_enablement table -- the first automated repository.py test in this
codebase (everything else is exercised through a FakeRepository test double at the call-site
layer, see tests/test_mcp_tools.py). Plugin enablement has no pure-logic caller yet in this
skeleton pass, so there's nothing else to verify these methods through -- a real SQLite-backed
test is the only way to catch a wrong SQLAlchemy Table/query, and it's fast/cheap enough that
this doesn't need a fake instead.

Enablement is scoped by owner_username only, not owner+vault -- a plugin, once on for an
account, applies across every vault that account owns. The vault a plugin resolver actually
needs is already known from the publish request's own path (/publish/{username}/{vault_id}/...)
at the point a plugin would run, so there's nothing to look up per-vault here.
"""

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def test_list_enabled_plugins_is_empty_for_unregistered_owner(repo):
    assert repo.list_enabled_plugins("alice") == []


def test_enable_plugin_then_list_returns_it(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)

    result = repo.list_enabled_plugins("alice")

    assert result == [{"plugin_id": "harang-immich", "enabled_at_ms": 1000}]


def test_enable_plugin_is_idempotent(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)
    repo.enable_plugin("alice", "harang-immich", 2000)

    result = repo.list_enabled_plugins("alice")

    assert result == [{"plugin_id": "harang-immich", "enabled_at_ms": 2000}]


def test_enable_plugin_scopes_by_owner(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)

    assert repo.list_enabled_plugins("bob") == []


def test_disable_plugin_removes_it(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)

    repo.disable_plugin("alice", "harang-immich")

    assert repo.list_enabled_plugins("alice") == []


def test_disable_plugin_is_a_no_op_when_not_enabled(repo):
    repo.disable_plugin("alice", "harang-immich")

    assert repo.list_enabled_plugins("alice") == []


def test_disable_plugin_only_affects_the_matching_owner_plugin(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)
    repo.enable_plugin("alice", "other-plugin", 1000)
    repo.enable_plugin("bob", "harang-immich", 1000)

    repo.disable_plugin("alice", "harang-immich")

    assert [p["plugin_id"] for p in repo.list_enabled_plugins("alice")] == ["other-plugin"]
    assert [p["plugin_id"] for p in repo.list_enabled_plugins("bob")] == ["harang-immich"]


def test_list_all_enabled_plugins_returns_every_registration(repo):
    repo.enable_plugin("alice", "harang-immich", 1000)
    repo.enable_plugin("bob", "other-plugin", 2000)

    result = repo.list_all_enabled_plugins()

    assert sorted(result, key=lambda r: r["owner_username"]) == [
        {"owner_username": "alice", "plugin_id": "harang-immich", "enabled_at_ms": 1000},
        {"owner_username": "bob", "plugin_id": "other-plugin", "enabled_at_ms": 2000},
    ]


def test_list_all_enabled_plugins_is_empty_initially(repo):
    assert repo.list_all_enabled_plugins() == []
