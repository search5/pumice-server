"""TDD for the plugin_enablement table -- the first automated repository.py test in this
codebase (everything else is exercised through a FakeRepository test double at the call-site
layer, see tests/test_mcp_tools.py). Plugin enablement has no pure-logic caller yet in this
skeleton pass, so there's nothing else to verify these methods through -- a real SQLite-backed
test is the only way to catch a wrong SQLAlchemy Table/query, and it's fast/cheap enough that
this doesn't need a fake instead.

Enablement is scoped by (owner_username, vault_id, plugin_id) -- this mirrors how Obsidian
itself scopes plugins per vault, not per account: a user might have pumice-plugin-immich installed and
enabled in one vault (where they actually embed Immich photos) but not in a completely
unrelated vault, and publishing that second vault must never run pumice-plugin-immich's syntax
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
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)

    result = repo.list_enabled_plugins("alice", "vault-1")

    assert result == [{"plugin_id": "pumice-plugin-immich", "enabled_at_ms": 1000}]


def test_enable_plugin_is_idempotent(repo):
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 2000)

    result = repo.list_enabled_plugins("alice", "vault-1")

    assert result == [{"plugin_id": "pumice-plugin-immich", "enabled_at_ms": 2000}]


def test_enable_plugin_scopes_by_owner(repo):
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)

    assert repo.list_enabled_plugins("bob", "vault-1") == []


def test_enable_plugin_scopes_by_vault_even_for_the_same_owner(repo):
    # The scenario the whole (owner, vault_id, plugin_id) key exists for: alice enables a
    # plugin in one vault, a completely different vault she also owns must not see it.
    repo.enable_plugin("alice", "vault-with-immich-photos", "pumice-plugin-immich", 1000)

    assert repo.list_enabled_plugins("alice", "unrelated-work-notes-vault") == []


def test_disable_plugin_removes_it(repo):
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)

    repo.disable_plugin("alice", "vault-1", "pumice-plugin-immich")

    assert repo.list_enabled_plugins("alice", "vault-1") == []


def test_disable_plugin_is_a_no_op_when_not_enabled(repo):
    repo.disable_plugin("alice", "vault-1", "pumice-plugin-immich")

    assert repo.list_enabled_plugins("alice", "vault-1") == []


def test_disable_plugin_only_affects_the_matching_owner_vault_plugin(repo):
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)
    repo.enable_plugin("alice", "vault-1", "other-plugin", 1000)
    repo.enable_plugin("alice", "vault-2", "pumice-plugin-immich", 1000)
    repo.enable_plugin("bob", "vault-1", "pumice-plugin-immich", 1000)

    repo.disable_plugin("alice", "vault-1", "pumice-plugin-immich")

    assert [p["plugin_id"] for p in repo.list_enabled_plugins("alice", "vault-1")] == ["other-plugin"]
    assert [p["plugin_id"] for p in repo.list_enabled_plugins("alice", "vault-2")] == ["pumice-plugin-immich"]
    assert [p["plugin_id"] for p in repo.list_enabled_plugins("bob", "vault-1")] == ["pumice-plugin-immich"]


def test_list_all_enabled_plugins_returns_every_registration(repo):
    repo.enable_plugin("alice", "vault-1", "pumice-plugin-immich", 1000)
    repo.enable_plugin("bob", "vault-2", "other-plugin", 2000)

    result = repo.list_all_enabled_plugins()

    assert sorted(result, key=lambda r: r["owner_username"]) == [
        {"owner_username": "alice", "vault_id": "vault-1", "plugin_id": "pumice-plugin-immich", "enabled_at_ms": 1000},
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


# plugin_config -- generic, opaque-to-the-repository storage for whatever a plugin needs to
# persist (e.g. pumice-plugin-immich's own Immich server_url/id/password per profile). The
# repository never sees plaintext or knows the shape of what's stored -- it's handed an already
# -encrypted blob (base64 text, see plugin_config_crypto.py) and stores/returns it verbatim.
# Scoped by (owner_username, vault_id, plugin_id, entry_id) -- entry_id lets one plugin store
# more than one named config (pumice-plugin-immich's multiple "profiles"), matching plugin_enablement's
# per-vault scope for the same reason: a plugin's config for one vault has nothing to do with a
# different vault the same owner happens to have.

def test_get_plugin_config_entries_is_empty_initially(repo):
    assert repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich") == {}


def test_set_plugin_config_entry_then_get_returns_it(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "encrypted-blob-1", 1000)

    result = repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich")

    assert result == {"profile-abc": {"config_blob_b64": "encrypted-blob-1", "updated_at_ms": 1000}}


def test_set_plugin_config_entry_is_idempotent_per_entry_id(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "encrypted-blob-1", 1000)
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "encrypted-blob-2", 2000)

    result = repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich")

    assert result == {"profile-abc": {"config_blob_b64": "encrypted-blob-2", "updated_at_ms": 2000}}


def test_set_plugin_config_entry_supports_multiple_entries_for_the_same_plugin(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "blob-abc", 1000)
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-def", "blob-def", 1000)

    result = repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich")

    assert result == {
        "profile-abc": {"config_blob_b64": "blob-abc", "updated_at_ms": 1000},
        "profile-def": {"config_blob_b64": "blob-def", "updated_at_ms": 1000},
    }


def test_plugin_config_scopes_by_vault_even_for_the_same_owner_and_plugin(repo):
    repo.set_plugin_config_entry("alice", "vault-with-immich", "pumice-plugin-immich", "profile-abc", "blob-1", 1000)

    assert repo.get_plugin_config_entries("alice", "unrelated-vault", "pumice-plugin-immich") == {}


def test_plugin_config_scopes_by_owner(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "blob-1", 1000)

    assert repo.get_plugin_config_entries("bob", "vault-1", "pumice-plugin-immich") == {}


def test_plugin_config_scopes_by_plugin_id(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "blob-1", 1000)

    assert repo.get_plugin_config_entries("alice", "vault-1", "some-other-plugin") == {}


def test_delete_plugin_config_entry_removes_it(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "blob-1", 1000)

    repo.delete_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc")

    assert repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich") == {}


def test_delete_plugin_config_entry_is_a_no_op_when_absent(repo):
    repo.delete_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc")

    assert repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich") == {}


def test_delete_plugin_config_entry_only_affects_the_matching_entry(repo):
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc", "blob-abc", 1000)
    repo.set_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-def", "blob-def", 1000)

    repo.delete_plugin_config_entry("alice", "vault-1", "pumice-plugin-immich", "profile-abc")

    result = repo.get_plugin_config_entries("alice", "vault-1", "pumice-plugin-immich")
    assert result == {"profile-def": {"config_blob_b64": "blob-def", "updated_at_ms": 1000}}
