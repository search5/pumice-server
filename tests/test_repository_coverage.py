"""Coverage follow-up for repository.py's SqlAlchemyMetadataRepository -- fills in gaps left by
tests/test_repository.py (plugin_enablement/oauth_clients/plugin_config/vault-size/purge) and the
various tests/test_service_*.py modules that exercise a real repo indirectly through the service
layer. Two areas of focus:

1. _migrate_to_owner_scoped_schema() -- the one-time legacy-DB migration that backfills
   owner_username onto a pre-owner-scoped database (see the big comment at the top of
   repository.py for the history of the bug this closes). This is real migration logic that
   runs against production data on every startup, so it's the highest-value thing to lock down
   here: happy path with a legacy vault_owner table present, the no-vault_owner-table fallback,
   tolerance of a malformed vault_owner table, and what actually happens (not what the comments
   merely claim happens) when a migration step fails partway through.

2. Scattered CRUD edge cases in various methods (rename, published files, users, device tokens,
   vault listing) that test_repository.py's plugin/oauth/config-focused tests never touched.

Same style as test_repository.py: plain functions, a `repo` fixture giving a fresh in-memory
SqlAlchemyMetadataRepository, no mocking framework beyond pytest's built-in monkeypatch (used only
to force the rare failure branches that require an actual DB-level failure to reach honestly).
"""

import pytest
from sqlalchemy import inspect as sa_inspect

from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


# ─── __init__ dialect branching ────────────────────────────────────────────────────────────────
# sqlite gets a StaticPool + check_same_thread=False (tested implicitly by every other test in
# this file). Every other dialect takes the pooled-engine branch instead (pool_size/max_overflow/
# pool_pre_ping) -- exercised here with a real (but unreachable) postgres URL. create_engine()
# itself doesn't touch the network (SQLAlchemy engines are lazy), so line execution reaches the
# pooled branch regardless; the actual connection attempt only happens once init_db() runs
# create_all(), which is what surfaces the failure below.

def test_non_sqlite_dialect_uses_pooled_engine_config_and_fails_to_connect():
    with pytest.raises(Exception):
        # Port 1 refuses immediately instead of timing out.
        SqlAlchemyMetadataRepository("postgresql://user:pass@localhost:1/db")


# ─── _migrate_to_owner_scoped_schema: happy path ───────────────────────────────────────────────
# Simulates a real pre-owner-scoping deployment: file_metadata/published_files keyed by
# (vault_id, path) only, file_history with no owner_username column, and a legacy vault_owner
# table recording who owned each vault_id. __init__ already runs init_db() once (creating the
# current, correct schema) -- so, mirroring test_repository.py's plugin_enablement migration
# test, the tables are dropped and recreated in the OLD shape afterwards and init_db() is called
# again explicitly, exactly as a real restart against a real pre-existing old-schema database
# would do.

def test_init_db_migrates_legacy_tables_using_the_vault_owner_table():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE file_metadata")
        conn.exec_driver_sql(
            "CREATE TABLE file_metadata ("
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "modified_at_ms BIGINT NOT NULL, size_bytes BIGINT NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, is_deleted BOOLEAN NOT NULL, "
            "PRIMARY KEY (vault_id, path))"
        )
        conn.exec_driver_sql("INSERT INTO file_metadata VALUES ('vault-1', 'a.md', 1000, 100, 'hash-a', 0)")

        conn.exec_driver_sql("DROP TABLE published_files")
        conn.exec_driver_sql(
            "CREATE TABLE published_files ("
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, PRIMARY KEY (vault_id, path))"
        )
        conn.exec_driver_sql("INSERT INTO published_files VALUES ('vault-1', 'pub.md', 'hash-pub')")

        conn.exec_driver_sql("DROP TABLE file_history")
        conn.exec_driver_sql(
            "CREATE TABLE file_history ("
            "history_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "modified_at_ms BIGINT NOT NULL, size_bytes BIGINT NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, backup_file_path VARCHAR(1024) NOT NULL, "
            "device_name VARCHAR(100) NOT NULL, user_name VARCHAR(100) NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO file_history (vault_id, path, modified_at_ms, size_bytes, content_hash, "
            "backup_file_path, device_name, user_name) VALUES "
            "('vault-1', 'a.md', 1000, 100, 'hash-a', '/backups/a.bak', 'dev', 'user')"
        )

        conn.exec_driver_sql(
            "CREATE TABLE vault_owner (vault_id VARCHAR(255) PRIMARY KEY, owner_username VARCHAR(100))"
        )
        conn.exec_driver_sql("INSERT INTO vault_owner VALUES ('vault-1', 'alice')")

    r.init_db()  # must not raise; migrates all three tables using vault_owner's mapping

    assert r.load_all("alice", "vault-1") == {
        "a.md": {
            "owner_username": "alice", "vault_id": "vault-1", "path": "a.md",
            "modified_at_ms": 1000, "size_bytes": 100, "content_hash": "hash-a", "is_deleted": False,
        }
    }
    assert r.get_published_files("alice", "vault-1") == [{"path": "pub.md", "hash": "hash-pub"}]

    history = r.get_history("alice", "vault-1", "a.md")
    assert len(history) == 1
    assert history[0]["owner_username"] == "alice"
    assert history[0]["backup_file_path"] == "/backups/a.bak"

    # And the schema now genuinely supports owner scoping going forward.
    r.save_one("bob", "vault-1", "a.md", {"modified_at_ms": 1, "size_bytes": 1, "content_hash": "x", "is_deleted": False})
    assert "a.md" in r.load_all("bob", "vault-1")
    assert "a.md" in r.load_all("alice", "vault-1")  # unaffected by bob's separate row


def test_init_db_migrates_file_metadata_to_blank_owner_when_no_vault_owner_table():
    # No vault_owner table at all -- a legacy DB that never had one, or one already cleaned up.
    # file_metadata/published_files still must be migrated (their primary key genuinely changed
    # shape) even though there's no way to know who owned each vault_id; they get "" instead.
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE file_metadata")
        conn.exec_driver_sql(
            "CREATE TABLE file_metadata ("
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "modified_at_ms BIGINT NOT NULL, size_bytes BIGINT NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, is_deleted BOOLEAN NOT NULL, "
            "PRIMARY KEY (vault_id, path))"
        )
        conn.exec_driver_sql("INSERT INTO file_metadata VALUES ('vault-2', 'b.md', 2000, 50, 'hash-b', 0)")

    r.init_db()

    result = r.load_all("", "vault-2")
    assert result["b.md"]["content_hash"] == "hash-b"
    assert result["b.md"]["owner_username"] == ""


def test_init_db_migration_tolerates_a_malformed_vault_owner_table():
    # vault_owner exists (so has_table() is True) but doesn't have the expected
    # (vault_id, owner_username) columns -- the inner try/except around the SELECT must catch
    # this and fall back to an empty mapping rather than blowing up the whole migration.
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE published_files")
        conn.exec_driver_sql(
            "CREATE TABLE published_files ("
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, PRIMARY KEY (vault_id, path))"
        )
        conn.exec_driver_sql("INSERT INTO published_files VALUES ('vault-3', 'c.md', 'hash-c')")
        conn.exec_driver_sql("CREATE TABLE vault_owner (id INTEGER PRIMARY KEY)")

    r.init_db()  # must not raise despite the malformed legacy table

    assert r.get_published_files("", "vault-3") == [{"path": "c.md", "hash": "hash-c"}]


def test_init_db_is_a_no_op_for_an_already_owner_scoped_database(repo):
    repo.save_one("alice", "vault-1", "a.md", {"modified_at_ms": 1, "size_bytes": 10, "content_hash": "h", "is_deleted": False})
    repo.add_history(
        owner_username="alice", vault_id="vault-1", path="a.md", modified_at_ms=1,
        size_bytes=10, content_hash="h", backup_file_path="/b", device_name="d", user_name="u",
    )

    repo.init_db()  # simulates a second server startup against an already-current-schema DB

    assert repo.load_all("alice", "vault-1")["a.md"]["content_hash"] == "h"
    history = repo.get_history("alice", "vault-1", "a.md")
    assert len(history) == 1
    assert history[0]["owner_username"] == "alice"


# ─── _migrate_to_owner_scoped_schema: failure branches ─────────────────────────────────────────
# These force a failure with monkeypatch since there's no way to make SQLite itself fail these
# specific statements honestly. Each documents what the except block *actually* does (as opposed
# to what the surrounding comments claim), per the standing rule to characterize real behavior.

def test_file_history_backfill_failure_is_caught_and_leaves_owner_username_unbackfilled(monkeypatch):
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE file_history")
        conn.exec_driver_sql(
            "CREATE TABLE file_history ("
            "history_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "modified_at_ms BIGINT NOT NULL, size_bytes BIGINT NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, backup_file_path VARCHAR(1024) NOT NULL, "
            "device_name VARCHAR(100) NOT NULL, user_name VARCHAR(100) NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO file_history (vault_id, path, modified_at_ms, size_bytes, content_hash, "
            "backup_file_path, device_name, user_name) VALUES "
            "('vault-1', 'a.md', 1, 10, 'h', '/b', 'd', 'u')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE vault_owner (vault_id VARCHAR(255) PRIMARY KEY, owner_username VARCHAR(100))"
        )
        conn.exec_driver_sql("INSERT INTO vault_owner VALUES ('vault-1', 'alice')")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated backfill failure")

    monkeypatch.setattr("pumice_server.repository.update", boom)

    r.init_db()  # the backfill failure must be caught and logged, not propagated

    # get_history()/get_history_by_id() are owner-scoped equality lookups, which can't match a
    # NULL owner_username, so read the raw column back to see what actually happened: the UPDATE
    # never ran, so the row is left exactly as unmigrated as it was before this init_db() call.
    with r.engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT owner_username FROM file_history WHERE path = 'a.md'").fetchone()
    assert row[0] is None


def test_file_metadata_recreate_failure_is_caught_but_loses_the_rows(monkeypatch):
    # Reveals a real gap between the code's comments and its actual behavior: the comment above
    # this loop in repository.py claims "a failure after the DROP still logs exactly what needs
    # restoring from a backup," but the except block only logs the exception message -- it never
    # logs the in-memory `transformed` rows it's holding. And despite the try/except wrapping the
    # whole `with self.engine.begin() as conn:` block (which would normally roll back on
    # exception), SQLite's DDL here is not actually rolled back: DROP TABLE + table.create()
    # persist, so the table ends up recreated with the *new* schema but empty -- the old rows are
    # gone, and the log has no way to recover them. This is a genuine data-loss risk on a failed
    # migration, not merely a cosmetic comment inaccuracy.
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE file_metadata")
        conn.exec_driver_sql(
            "CREATE TABLE file_metadata ("
            "vault_id VARCHAR(255) NOT NULL, path VARCHAR(500) NOT NULL, "
            "modified_at_ms BIGINT NOT NULL, size_bytes BIGINT NOT NULL, "
            "content_hash VARCHAR(64) NOT NULL, is_deleted BOOLEAN NOT NULL, "
            "PRIMARY KEY (vault_id, path))"
        )
        conn.exec_driver_sql("INSERT INTO file_metadata VALUES ('vault-1', 'a.md', 1, 10, 'h', 0)")

    from sqlalchemy import insert as real_insert

    def boom(table):
        if table.name == "file_metadata":
            raise RuntimeError("simulated reinsert failure")
        return real_insert(table)

    monkeypatch.setattr("pumice_server.repository.insert", boom)

    r.init_db()  # must not raise -- the failure is caught and logged, not propagated

    assert sa_inspect(r.engine).has_table("file_metadata")
    assert [c["name"] for c in sa_inspect(r.engine).get_columns("file_metadata")][0] == "owner_username"
    assert r.load_all("", "vault-1") == {}  # the pre-migration row is simply gone


def test_plugin_enablement_recreate_failure_is_caught_but_leaves_the_table_dropped(monkeypatch):
    # Same shape of failure as above but for _migrate_plugin_enablement_to_vault_scoped_schema:
    # DROP TABLE succeeds, then table.create() itself fails. The exception is swallowed
    # (non-fatal per the log message) but the table is left entirely missing -- worse than the
    # file_metadata case, since every subsequent plugin_enablement call now hits "no such table"
    # instead of merely losing rows.
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    with r.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE plugin_enablement")
        conn.exec_driver_sql(
            "CREATE TABLE plugin_enablement ("
            "owner_username VARCHAR(100) NOT NULL, plugin_id VARCHAR(255) NOT NULL, "
            "enabled_at_ms BIGINT NOT NULL, PRIMARY KEY (owner_username, plugin_id))"
        )
        conn.exec_driver_sql("INSERT INTO plugin_enablement VALUES ('alice', 'old-plugin', 1000)")

    def boom(conn):
        raise RuntimeError("simulated create failure")

    monkeypatch.setattr(r._plugin_enablement, "create", boom)

    r.init_db()  # must not raise

    assert not sa_inspect(r.engine).has_table("plugin_enablement")
    with pytest.raises(Exception):
        r.list_all_enabled_plugins()  # confirms the table is genuinely gone, not just empty


# ─── migrate_history_on_rename ─────────────────────────────────────────────────────────────────

def test_migrate_history_on_rename_updates_both_history_and_metadata_paths(repo):
    repo.save_one("alice", "vault-1", "old.md", {"modified_at_ms": 1, "size_bytes": 10, "content_hash": "h", "is_deleted": False})
    repo.add_history(
        owner_username="alice", vault_id="vault-1", path="old.md", modified_at_ms=1,
        size_bytes=10, content_hash="h", backup_file_path="/b", device_name="d", user_name="u",
    )

    repo.migrate_history_on_rename("alice", "vault-1", "old.md", "new.md")

    assert repo.load_one("alice", "vault-1", "old.md") is None
    assert repo.load_one("alice", "vault-1", "new.md")["content_hash"] == "h"
    assert repo.get_history("alice", "vault-1", "old.md") == []
    renamed_history = repo.get_history("alice", "vault-1", "new.md")
    assert len(renamed_history) == 1
    assert renamed_history[0]["backup_file_path"] == "/b"


def test_migrate_history_on_rename_only_affects_the_matching_owner_and_vault(repo):
    repo.save_one("alice", "vault-1", "shared-name.md", {"modified_at_ms": 1, "size_bytes": 10, "content_hash": "h1", "is_deleted": False})
    repo.save_one("bob", "vault-1", "shared-name.md", {"modified_at_ms": 1, "size_bytes": 20, "content_hash": "h2", "is_deleted": False})

    repo.migrate_history_on_rename("alice", "vault-1", "shared-name.md", "renamed.md")

    assert repo.load_one("alice", "vault-1", "renamed.md")["content_hash"] == "h1"
    assert repo.load_one("bob", "vault-1", "shared-name.md")["content_hash"] == "h2"
    assert repo.load_one("bob", "vault-1", "renamed.md") is None


def test_migrate_history_on_rename_is_a_no_op_when_the_old_path_does_not_exist(repo):
    repo.migrate_history_on_rename("alice", "vault-1", "missing.md", "new.md")  # must not raise

    assert repo.load_all("alice", "vault-1") == {}


# ─── add_published_file / remove_published_file / get_published_files ─────────────────────────

def test_get_published_files_is_empty_initially(repo):
    assert repo.get_published_files("alice", "vault-1") == []


def test_add_published_file_then_get_returns_it(repo):
    repo.add_published_file("alice", "vault-1", "note.md", "hash-1")

    assert repo.get_published_files("alice", "vault-1") == [{"path": "note.md", "hash": "hash-1"}]


def test_add_published_file_upserts_the_hash_for_the_same_path(repo):
    repo.add_published_file("alice", "vault-1", "note.md", "hash-1")
    repo.add_published_file("alice", "vault-1", "note.md", "hash-2")

    assert repo.get_published_files("alice", "vault-1") == [{"path": "note.md", "hash": "hash-2"}]


def test_get_published_files_scopes_by_owner_and_vault(repo):
    repo.add_published_file("alice", "vault-1", "a.md", "h1")
    repo.add_published_file("alice", "vault-2", "b.md", "h2")
    repo.add_published_file("bob", "vault-1", "c.md", "h3")

    assert repo.get_published_files("alice", "vault-1") == [{"path": "a.md", "hash": "h1"}]


def test_remove_published_file_removes_only_the_matching_row(repo):
    repo.add_published_file("alice", "vault-1", "a.md", "h1")
    repo.add_published_file("alice", "vault-1", "b.md", "h2")
    repo.add_published_file("bob", "vault-1", "a.md", "h3")

    repo.remove_published_file("alice", "vault-1", "a.md")

    remaining = {p["path"] for p in repo.get_published_files("alice", "vault-1")}
    assert remaining == {"b.md"}
    assert repo.get_published_files("bob", "vault-1") == [{"path": "a.md", "hash": "h3"}]


def test_remove_published_file_is_a_no_op_when_absent(repo):
    repo.remove_published_file("alice", "vault-1", "missing.md")  # must not raise

    assert repo.get_published_files("alice", "vault-1") == []


# ─── create_user / get_user_by_username ────────────────────────────────────────────────────────

def test_create_user_succeeds_for_a_new_username(repo):
    created = repo.create_user("alice", "hashed-pw")

    assert created is True
    user = repo.get_user_by_username("alice")
    assert user["username"] == "alice"
    assert user["password_hash"] == "hashed-pw"


def test_create_user_with_optional_fields(repo):
    repo.create_user("alice", "hashed-pw", name="Alice", email="alice@example.com", is_admin=True)

    user = repo.get_user_by_username("alice")
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"
    assert bool(user["is_admin"]) is True


def test_create_user_returns_false_and_does_not_overwrite_an_existing_username(repo):
    repo.create_user("alice", "first-hash")

    created_again = repo.create_user("alice", "second-hash")

    assert created_again is False
    assert repo.get_user_by_username("alice")["password_hash"] == "first-hash"


def test_get_user_by_username_returns_none_when_absent(repo):
    assert repo.get_user_by_username("nobody") is None


# ─── device tokens: list_device_tokens / delete_device_token ──────────────────────────────────

def test_list_device_tokens_is_empty_initially(repo):
    assert repo.list_device_tokens("alice") == []


def test_list_device_tokens_returns_only_the_matching_username(repo):
    repo.create_device_token("tok-1", "alice", "iPhone", 1000)
    repo.create_device_token("tok-2", "alice", "iPad", 2000)
    repo.create_device_token("tok-3", "bob", "Android", 3000)

    tokens = repo.list_device_tokens("alice")

    assert {t["token"] for t in tokens} == {"tok-1", "tok-2"}
    assert all(t["username"] == "alice" for t in tokens)


def test_delete_device_token_removes_only_the_matching_token(repo):
    repo.create_device_token("tok-1", "alice", "iPhone", 1000)
    repo.create_device_token("tok-2", "alice", "iPad", 2000)

    repo.delete_device_token("tok-1")

    assert repo.get_device_token("tok-1") is None
    assert repo.get_device_token("tok-2") is not None
    assert [t["token"] for t in repo.list_device_tokens("alice")] == ["tok-2"]


def test_delete_device_token_is_a_no_op_when_absent(repo):
    repo.delete_device_token("no-such-token")  # must not raise


# ─── get_vaults_by_owner ────────────────────────────────────────────────────────────────────────

def test_get_vaults_by_owner_is_empty_for_an_unknown_owner(repo):
    assert repo.get_vaults_by_owner("alice") == []


def test_get_vaults_by_owner_unions_file_metadata_and_published_files_vaults(repo):
    repo.save_one("alice", "vault-metadata-only", "a.md", {"modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False})
    repo.add_published_file("alice", "vault-published-only", "b.md", "h2")
    repo.save_one("alice", "vault-both", "c.md", {"modified_at_ms": 1, "size_bytes": 1, "content_hash": "h3", "is_deleted": False})
    repo.add_published_file("alice", "vault-both", "c.md", "h3")

    assert repo.get_vaults_by_owner("alice") == ["vault-both", "vault-metadata-only", "vault-published-only"]


def test_get_vaults_by_owner_scopes_by_owner(repo):
    repo.save_one("alice", "vault-1", "a.md", {"modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False})
    repo.add_published_file("bob", "vault-2", "b.md", "h2")

    assert repo.get_vaults_by_owner("alice") == ["vault-1"]
    assert repo.get_vaults_by_owner("bob") == ["vault-2"]


# ─── get_all_users / delete_user / update_user_password / update_user_profile ─────────────────

def test_get_all_users_is_empty_initially(repo):
    assert repo.get_all_users() == []


def test_get_all_users_returns_every_user_with_expected_fields(repo):
    repo.create_user("alice", "hash-a", name="Alice", email="alice@example.com", is_admin=True)
    repo.create_user("bob", "hash-b")

    users = {u["username"]: u for u in repo.get_all_users()}

    assert users["alice"] == {"username": "alice", "name": "Alice", "email": "alice@example.com", "is_admin": True}
    assert users["bob"] == {"username": "bob", "name": None, "email": None, "is_admin": False}


def test_delete_user_removes_only_the_matching_username(repo):
    repo.create_user("alice", "hash-a")
    repo.create_user("bob", "hash-b")

    repo.delete_user("alice")

    assert repo.get_user_by_username("alice") is None
    assert repo.get_user_by_username("bob") is not None


def test_delete_user_is_a_no_op_when_absent(repo):
    repo.delete_user("nobody")  # must not raise


def test_update_user_password_changes_only_the_password_hash(repo):
    repo.create_user("alice", "old-hash", name="Alice", email="alice@example.com")

    repo.update_user_password("alice", "new-hash")

    user = repo.get_user_by_username("alice")
    assert user["password_hash"] == "new-hash"
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"


def test_update_user_profile_changes_name_and_email_only(repo):
    repo.create_user("alice", "hash-a", name="Old Name", email="old@example.com")

    repo.update_user_profile("alice", "New Name", "new@example.com")

    user = repo.get_user_by_username("alice")
    assert user["name"] == "New Name"
    assert user["email"] == "new@example.com"
    assert user["password_hash"] == "hash-a"
