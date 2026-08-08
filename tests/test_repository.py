"""TDD for the plugin_enablement table -- the first automated repository.py test in this
codebase (everything else is exercised through a FakeRepository test double at the call-site
layer, see tests/test_mcp_tools.py). Plugin enablement has no pure-logic caller yet in this
skeleton pass, so there's nothing else to verify these methods through -- a real SQLite-backed
test is the only way to catch a wrong SQLAlchemy Table/query, and it's fast/cheap enough that
this doesn't need a fake instead.

Enablement is scoped by (owner_username, vault_id, plugin_id) -- this mirrors how Obsidian
itself scopes plugins per vault, not per account: a user might have harang-immich installed and
enabled in one vault (where they actually embed Immich photos) but not in a completely
unrelated vault, and publishing that second vault must never run harang-immich's syntax
resolution over it just because the account happens to have the plugin on elsewhere. This was
initially scoped by owner_username only (a plugin, once on for an account, applied everywhere
that account owned) -- reverted after realizing that reasoning only holds for *installation*
(a server-wide, account-independent concept: the code either exists in the image or it doesn't)
and not for *enablement*, which is a genuine per-vault choice with no way to derive "should this
vault's publish rendering run this plugin at all" from context the way a resolver's actual
target vault can be derived from the request path.
"""

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def test_list_enabled_plugins_is_empty_for_unregistered_vault(repo):
    assert repo.list_enabled_plugins("alice", "vault-1") == []


def test_enable_plugin_then_list_returns_it(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)

    result = repo.list_enabled_plugins("alice", "vault-1")

    assert result == [{"plugin_id": "harang-immich", "enabled_at_ms": 1000}]


def test_enable_plugin_is_idempotent(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)
    repo.enable_plugin("alice", "vault-1", "harang-immich", 2000)

    result = repo.list_enabled_plugins("alice", "vault-1")

    assert result == [{"plugin_id": "harang-immich", "enabled_at_ms": 2000}]


def test_enable_plugin_scopes_by_owner(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)

    assert repo.list_enabled_plugins("bob", "vault-1") == []


def test_enable_plugin_scopes_by_vault_even_for_the_same_owner(repo):
    # The scenario the whole (owner, vault_id, plugin_id) key exists for: alice enables a
    # plugin in one vault, a completely different vault she also owns must not see it.
    repo.enable_plugin("alice", "vault-with-immich-photos", "harang-immich", 1000)

    assert repo.list_enabled_plugins("alice", "unrelated-work-notes-vault") == []


def test_disable_plugin_removes_it(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)

    repo.disable_plugin("alice", "vault-1", "harang-immich")

    assert repo.list_enabled_plugins("alice", "vault-1") == []


def test_disable_plugin_is_a_no_op_when_not_enabled(repo):
    repo.disable_plugin("alice", "vault-1", "harang-immich")

    assert repo.list_enabled_plugins("alice", "vault-1") == []


def test_disable_plugin_only_affects_the_matching_owner_vault_plugin(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)
    repo.enable_plugin("alice", "vault-1", "other-plugin", 1000)
    repo.enable_plugin("alice", "vault-2", "harang-immich", 1000)
    repo.enable_plugin("bob", "vault-1", "harang-immich", 1000)

    repo.disable_plugin("alice", "vault-1", "harang-immich")

    assert [p["plugin_id"] for p in repo.list_enabled_plugins("alice", "vault-1")] == ["other-plugin"]
    assert [p["plugin_id"] for p in repo.list_enabled_plugins("alice", "vault-2")] == ["harang-immich"]
    assert [p["plugin_id"] for p in repo.list_enabled_plugins("bob", "vault-1")] == ["harang-immich"]


def test_list_all_enabled_plugins_returns_every_registration(repo):
    repo.enable_plugin("alice", "vault-1", "harang-immich", 1000)
    repo.enable_plugin("bob", "vault-2", "other-plugin", 2000)

    result = repo.list_all_enabled_plugins()

    assert sorted(result, key=lambda r: r["owner_username"]) == [
        {"owner_username": "alice", "vault_id": "vault-1", "plugin_id": "harang-immich", "enabled_at_ms": 1000},
        {"owner_username": "bob", "vault_id": "vault-2", "plugin_id": "other-plugin", "enabled_at_ms": 2000},
    ]


def test_list_all_enabled_plugins_is_empty_initially(repo):
    assert repo.list_all_enabled_plugins() == []


# oauth_clients -- dynamic client registration (RFC 7591) for the MCP browser-auth flow. Public
# clients only (PKCE, no secret), so redirect_uris round-tripping correctly is the main thing
# worth a real SQLite-backed test here (it's the one column that isn't a plain scalar).

def test_get_oauth_client_returns_none_when_not_registered(repo):
    assert repo.get_oauth_client("no-such-client") is None


def test_create_oauth_client_then_get_round_trips_redirect_uris(repo):
    repo.create_oauth_client("client-1", "Claude", ["http://localhost:1234/callback", "http://localhost:5678/callback"], 1000)

    client = repo.get_oauth_client("client-1")

    assert client["client_id"] == "client-1"
    assert client["client_name"] == "Claude"
    assert client["redirect_uris"] == ["http://localhost:1234/callback", "http://localhost:5678/callback"]
    assert client["created_at_ms"] == 1000
