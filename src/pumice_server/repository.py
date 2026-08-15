from abc import ABC, abstractmethod
from contextlib import contextmanager
import os
import json
import logging
import time
import uuid
from typing import Callable, Dict, Any, Optional

from sqlalchemy import (
    create_engine, MetaData, Table, Column, Index, inspect,
    BigInteger, Integer, String, Boolean, Text,
    select, insert, update, delete, func,
)
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# Every vault-scoped method below takes owner_username as its first argument. Vault identity is
# (owner_username, vault_id), not vault_id alone -- vault_id is just whatever the Obsidian client's
# vault is locally named (vault.getName()), which is not globally unique (a great many vaults are
# literally named "Obsidian Vault", the default). Keying storage on vault_id alone meant two
# different accounts syncing a vault with the same name would collide: whichever synced first
# permanently claimed that name and the second was locked out. Scoping by owner from the moment a
# vault's data is first written makes that collision structurally impossible -- two accounts'
# "Obsidian Vault" are simply different rows/paths from the start, no "claim" step needed. This
# also closes a related bug: get_history_by_id() used to look up a history row by its bare
# (global, cross-vault) history_id with no check that it belonged to the vault the caller was
# authorized for, so owning any one vault let you enumerate history_ids and pull file content from
# other people's vaults. It now requires the caller's owner_username to match too.


class VaultAccessDenied(Exception):
    """Raised by resolve_vault_owner() when the caller has no right to act as the requested
    owner for this vault -- they're not that owner themselves, and no vault_shares row matches
    their account's own email for (requested_owner_username, vault_id). See
    pumice's 14_vault_sharing_설계.md for the full design."""


class MetadataRepository(ABC):
    @abstractmethod
    def init_db(self) -> None:
        pass

    @abstractmethod
    def load_all(self, owner_username: str, vault_id: str) -> Dict[str, Dict[str, Any]]:
        pass

    @abstractmethod
    def load_one(self, owner_username: str, vault_id: str, path: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def save_all(self, owner_username: str, vault_id: str, files_meta: Dict[str, Dict[str, Any]]) -> None:
        pass

    @abstractmethod
    def save_one(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def add_history(self, owner_username: str, vault_id: str, path: str, modified_at_ms: int,
                    size_bytes: int, content_hash: str, backup_file_path: str,
                    device_name: str, user_name: str,
                    deleted: bool = False, related_path: Optional[str] = None) -> None:
        pass

    @abstractmethod
    def record_change(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        """Appends one row to the vault-wide change journal (`vault_change_log`) -- every call
        site that mutates file_metadata must also call this with the same 5-key meta dict
        (path/modified_at_ms/size_bytes/content_hash/is_deleted) passed to save_one/save_all, so
        a client with a lastKnownChangeId baseline can later ask "what changed since then"
        without a full-vault Delta scan. See #14_옵시디언싱크_정렬_구현계획.md (pumice repo)."""
        pass

    @abstractmethod
    def batch(self):
        """Context manager yielding an object with save_one/add_history that share a
        single commit, instead of each call committing (and fsyncing) on its own."""
        pass

    @abstractmethod
    def get_changes_since(self, owner_username: str, vault_id: str, since_change_id: int) -> list:
        """Returns one FileMeta-shaped dict (path/modified_at_ms/size_bytes/content_hash/
        is_deleted) per path whose vault_change_log entry has change_id > since_change_id,
        folded to each path's latest recorded state -- PR2's `init` handshake catch-up burst
        (see #14_옵시디언싱크_정렬_구현계획.md, pumice repo). Nothing calls this with
        since_change_id=0 in practice (that case skips catch-up entirely client-side), but it's
        valid and just returns every recorded change for the vault."""
        pass

    @abstractmethod
    def get_latest_change_id(self, owner_username: str, vault_id: str) -> int:
        """The highest change_id recorded for (owner, vault), or 0 if none yet -- sent back in
        the `ready` op's payload so the client can set its new lastKnownChangeId baseline."""
        pass

    @abstractmethod
    def find_live_file_by_content_hash(self, owner_username: str, vault_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        """PR6 of #14_옵시디언싱크_정렬_구현계획.md: content-addressed upload dedup. Returns one
        live (is_deleted=False) file_metadata row whose content_hash matches, or None -- scoped
        to (owner, vault), never cross-vault/cross-owner (see the caller for why). Always None
        for content_hash="" (the tombstone/placeholder sentinel elsewhere in this codebase, not
        a real hash)."""
        pass

    @abstractmethod
    def get_history(self, owner_username: str, vault_id: str, path: str) -> list:
        pass

    @abstractmethod
    def get_history_by_id(self, owner_username: str, history_id: int) -> Optional[dict]:
        pass

    @abstractmethod
    def add_published_file(self, owner_username: str, vault_id: str, path: str, content_hash: str) -> None:
        pass

    @abstractmethod
    def migrate_history_on_rename(self, owner_username: str, vault_id: str, old_path: str, new_path: str) -> None:
        pass

    @abstractmethod
    def remove_published_file(self, owner_username: str, vault_id: str, path: str) -> None:
        pass

    @abstractmethod
    def get_published_files(self, owner_username: str, vault_id: str) -> list:
        pass

    @abstractmethod
    def enable_plugin(self, owner_username: str, vault_id: str, plugin_id: str, enabled_at_ms: int) -> None:
        pass

    @abstractmethod
    def disable_plugin(self, owner_username: str, vault_id: str, plugin_id: str) -> None:
        pass

    @abstractmethod
    def list_enabled_plugins(self, owner_username: str, vault_id: str) -> list:
        pass

    @abstractmethod
    def list_all_enabled_plugins(self) -> list:
        pass

    @abstractmethod
    def create_user(self, username: str, password_hash: str) -> bool:
        pass

    @abstractmethod
    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def create_device_token(self, token: str, username: str, device_name: str, created_at_ms: int) -> None:
        pass

    @abstractmethod
    def get_device_token(self, token: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def list_device_tokens(self, username: str) -> list:
        pass

    @abstractmethod
    def delete_device_token(self, token: str) -> None:
        pass

    @abstractmethod
    def create_oauth_client(self, client_id: str, client_name: str, redirect_uris: list, created_at_ms: int) -> None:
        pass

    @abstractmethod
    def get_oauth_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def set_plugin_config_entry(self, owner_username: str, vault_id: str, plugin_id: str, entry_id: str, config_blob_b64: str, updated_at_ms: int) -> None:
        pass

    @abstractmethod
    def get_plugin_config_entries(self, owner_username: str, vault_id: str, plugin_id: str) -> dict:
        pass

    @abstractmethod
    def delete_plugin_config_entry(self, owner_username: str, vault_id: str, plugin_id: str, entry_id: str) -> None:
        pass

    @abstractmethod
    def get_vaults_by_owner(self, owner_username: str) -> list:
        pass

    @abstractmethod
    def get_vault_size_bytes(self, owner_username: str, vault_id: str) -> int:
        pass

    @abstractmethod
    def get_total_size_bytes(self, owner_username: str) -> int:
        pass

    @abstractmethod
    def purge_vault(self, owner_username: str, vault_id: str) -> list:
        pass

    @abstractmethod
    def get_all_users(self) -> list:
        pass

    @abstractmethod
    def delete_user(self, username: str) -> None:
        pass

    @abstractmethod
    def update_user_password(self, username: str, password_hash: str) -> None:
        pass

    @abstractmethod
    def update_user_profile(self, username: str, name: str, email: str) -> None:
        pass

    @abstractmethod
    def add_vault_share(self, owner_username: str, vault_id: str, email: str) -> str:
        pass

    @abstractmethod
    def list_vault_shares(self, owner_username: str, vault_id: str) -> list:
        pass

    @abstractmethod
    def remove_vault_share(self, owner_username: str, vault_id: str, share_uid: str) -> bool:
        pass

    @abstractmethod
    def remove_vault_share_by_invited_user(self, caller_username: str, owner_username: str, vault_id: str) -> bool:
        pass

    @abstractmethod
    def list_shared_vaults_for_user(self, username: str) -> list:
        pass

    @abstractmethod
    def get_accepted_share_usernames(self, owner_username: str, vault_id: str) -> list:
        pass

    @abstractmethod
    def resolve_vault_owner(self, caller_username: str, requested_owner_username: str, vault_id: str,
                             mark_accepted: bool = False) -> str:
        pass


class SqlAlchemyMetadataRepository(MetadataRepository):
    def __init__(self, connection_url: str):
        dialect = connection_url.split("://")[0].split("+")[0]

        if dialect == "sqlite":
            self.engine = create_engine(
                connection_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self.engine = create_engine(
                connection_url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )

        self._meta = MetaData()
        self._define_tables()
        self.init_db()

    def _define_tables(self):
        self._file_metadata = Table("file_metadata", self._meta,
            Column("owner_username", String(100), primary_key=True),
            Column("vault_id", String(255), primary_key=True),
            Column("path", String(500), primary_key=True),
            Column("modified_at_ms", BigInteger, nullable=False),
            Column("size_bytes", BigInteger, nullable=False),
            Column("content_hash", String(64), nullable=False),
            Column("is_deleted", Boolean, nullable=False, default=False),
        )

        self._file_history = Table("file_history", self._meta,
            Column("history_id", Integer, primary_key=True, autoincrement=True),
            Column("owner_username", String(100), nullable=False, default=""),
            Column("vault_id", String(255), nullable=False),
            Column("path", String(500), nullable=False),
            Column("modified_at_ms", BigInteger, nullable=False),
            Column("size_bytes", BigInteger, nullable=False),
            Column("content_hash", String(64), nullable=False),
            Column("backup_file_path", String(1024), nullable=False),
            Column("device_name", String(100), nullable=False, default=""),
            Column("user_name", String(100), nullable=False, default=""),
            Column("deleted", Boolean, nullable=False, default=False),
            Column("related_path", String(500), nullable=True),
        )
        Index("idx_history_owner_vault_path", self._file_history.c.owner_username,
              self._file_history.c.vault_id, self._file_history.c.path)

        # Append-only vault-wide change journal (PR1 of #14_옵시디언싱크_정렬_구현계획.md, pumice
        # repo) -- unlike file_history above, this is never rewritten (no rename path-mutation)
        # and never carries a physical backup, purely a metadata event log. change_id is a
        # global (cross-vault) autoincrement, same pattern as file_history.history_id -- queries
        # always filter by (owner_username, vault_id) so this is fine, and it's already proven
        # safe across all 4 supported dialects (see idx_history_owner_vault_path above). Nothing
        # reads this table yet (that's PR2, the `init` handshake catch-up) -- this PR only wires
        # up writing to it.
        self._vault_change_log = Table("vault_change_log", self._meta,
            Column("change_id", Integer, primary_key=True, autoincrement=True),
            Column("owner_username", String(100), nullable=False),
            Column("vault_id", String(255), nullable=False),
            Column("path", String(500), nullable=False),
            Column("content_hash", String(64), nullable=False),
            Column("size_bytes", BigInteger, nullable=False),
            Column("modified_at_ms", BigInteger, nullable=False),
            Column("is_deleted", Boolean, nullable=False, default=False),
            Column("recorded_at_ms", BigInteger, nullable=False),
        )
        Index("idx_change_log_owner_vault_change_id", self._vault_change_log.c.owner_username,
              self._vault_change_log.c.vault_id, self._vault_change_log.c.change_id)

        self._published_files = Table("published_files", self._meta,
            Column("owner_username", String(100), primary_key=True),
            Column("vault_id", String(255), primary_key=True),
            Column("path", String(500), primary_key=True),
            Column("content_hash", String(64), nullable=False),
        )

        self._users = Table("users", self._meta,
            Column("username", String(100), primary_key=True),
            Column("password_hash", String(255), nullable=False),
            Column("token", String(255), nullable=True),
            Column("name", String(100), nullable=True),
            Column("email", String(255), nullable=True),
            Column("is_admin", Boolean, nullable=False, default=False),
        )

        self._device_tokens = Table("device_tokens", self._meta,
            Column("token", String(255), primary_key=True),
            Column("username", String(100), nullable=False),
            Column("device_name", String(100), nullable=False, default=""),
            Column("created_at_ms", BigInteger, nullable=False),
        )
        Index("idx_device_tokens_username", self._device_tokens.c.username)

        # OAuth 2.1 dynamic client registration (RFC 7591) for the MCP browser-auth flow -- an
        # MCP client (Claude Code/Desktop, ...) self-registers here before ever asking a user to
        # approve anything, so "just paste the server URL" works without anyone manually handing
        # out a client_id out of band. Public clients only (PKCE, no client_secret), so there's
        # nothing sensitive in this table. redirect_uris is stored JSON-encoded since a client
        # can register more than one (e.g. several loopback ports it might bind its local
        # callback server to).
        self._oauth_clients = Table("oauth_clients", self._meta,
            Column("client_id", String(64), primary_key=True),
            Column("client_name", String(200), nullable=True),
            Column("redirect_uris", Text, nullable=False),
            Column("created_at_ms", BigInteger, nullable=False),
        )

        # Which server-side publish plugins (plugin_registry.py) are turned on for a given
        # vault -- a plugin can be baked into the server image (declared as a build-time
        # pumice-server dependency, discoverable via discover_plugins()) without being enabled
        # anywhere; a row here is what actually lets it run when that vault gets published.
        # Scoped by (owner_username, vault_id, plugin_id), mirroring how Obsidian itself scopes
        # plugins per vault, not per account -- a user might have pumice-plugin-immich enabled in one
        # vault (where they actually embed Immich photos) but not in an unrelated vault they
        # also own, and publishing that second vault must never run pumice-plugin-immich's syntax
        # resolution over it just because the account has the plugin on elsewhere. This was
        # originally scoped by owner_username alone; reverted after realizing that reasoning
        # only holds for *installation* (a server-wide, account-independent concept -- the code
        # either exists in the image or it doesn't), not *enablement*, which is a genuine
        # per-vault choice with no way to derive "should this vault run this plugin at all"
        # from request context the way a resolver's actual target vault can be.
        self._plugin_enablement = Table("plugin_enablement", self._meta,
            Column("owner_username", String(100), primary_key=True),
            Column("vault_id", String(255), primary_key=True),
            Column("plugin_id", String(255), primary_key=True),
            Column("enabled_at_ms", BigInteger, nullable=False),
        )

        # Generic, opaque-to-the-repository config storage for whatever a plugin needs to
        # persist -- e.g. pumice-plugin-immich's own Immich server_url/token, entered
        # once server-side and read back on every publish render (see plugin_config_crypto.py).
        # The repository never sees plaintext or knows the shape of what's stored; config_blob
        # is already-encrypted, base64-encoded text by the time it gets here. Stored as TEXT
        # rather than a binary column since this codebase supports several DB backends
        # (sqlite/mysql/mariadb/postgresql/cubrid) and binary-column handling isn't uniformly
        # reliable across all of their drivers -- base64's size overhead is irrelevant for a
        # blob this small. Scoped by (owner_username, vault_id, plugin_id, entry_id): entry_id
        # lets one plugin store more than one named config (pumice-plugin-immich's multiple
        # "profiles"), and the rest mirrors plugin_enablement's per-vault scope for the same
        # reason -- a plugin's config for one vault has nothing to do with a different vault the
        # same owner happens to have.
        self._plugin_config = Table("plugin_config", self._meta,
            Column("owner_username", String(100), primary_key=True),
            Column("vault_id", String(255), primary_key=True),
            Column("plugin_id", String(255), primary_key=True),
            Column("entry_id", String(255), primary_key=True),
            Column("config_blob", Text, nullable=False),
            Column("updated_at_ms", BigInteger, nullable=False),
        )

        # Vault sync sharing (see pumice's 14_vault_sharing_설계.md) -- an owner invites another
        # account, by email, to get full read/write sync access to one of their vaults. Deliberately
        # a real indexed table rather than the JSON-file-per-vault pattern web.py's existing
        # /publish/share/* uses for Publish's own (unrelated) site-viewer sharing: this table is
        # read on every WS connect (resolve_vault_owner below), not just an occasional REST call.
        # email is stored lowercased; matching against users.email is always done case-insensitively
        # too, so an invite survives the invited account's email being entered with different casing.
        # No unique DB constraint on (owner_username, vault_id, email) -- add_vault_share() enforces
        # "no duplicate pending invite" in Python instead, same style create_user() already uses for
        # its own existence check.
        self._vault_shares = Table("vault_shares", self._meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("uid", String(36), nullable=False),
            Column("owner_username", String(100), nullable=False),
            Column("vault_id", String(255), nullable=False),
            Column("email", String(255), nullable=False),
            Column("accepted", Boolean, nullable=False, default=False),
            Column("created_at_ms", BigInteger, nullable=False),
            Column("accepted_at_ms", BigInteger, nullable=True),
        )
        Index("idx_vault_shares_owner_vault", self._vault_shares.c.owner_username,
              self._vault_shares.c.vault_id)
        Index("idx_vault_shares_email", self._vault_shares.c.email)
        Index("idx_vault_shares_uid", self._vault_shares.c.uid, unique=True)

    def init_db(self) -> None:
        # Runs before the owner-scoping migration below: file_metadata/published_files may still
        # need to be dropped and recreated (their primary key changed shape), so create_all() has
        # to happen first to guarantee file_history and the other untouched tables exist, and the
        # migration step below handles file_metadata/published_files on its own.
        self._meta.create_all(self.engine)

        # Safely backfill new columns (deleted, related_path) onto a pre-existing file_history table.
        # The column type name isn't hardcoded — it's obtained from SQLAlchemy's type compiler per
        # dialect (e.g. CUBRID compiles Boolean to SMALLINT; using "BOOLEAN" literally in ALTER TABLE
        # would be a syntax error on CUBRID). Each column is run in its own transaction so a failure
        # on one doesn't affect the other.
        for col in (self._file_history.c.deleted, self._file_history.c.related_path, self._file_history.c.owner_username):
            col_type = col.type.compile(dialect=self.engine.dialect)
            stmt = f"ALTER TABLE file_history ADD COLUMN {col.name} {col_type}"
            try:
                with self.engine.begin() as conn:
                    conn.exec_driver_sql(stmt)
            except Exception:
                pass  # the column already exists

        self._migrate_to_owner_scoped_schema()
        self._migrate_plugin_enablement_to_vault_scoped_schema()

    def _replace_table_safely(self, conn, name: str, new_table: Table, populate: Callable[[Any], None]) -> None:
        """Swaps `name`'s table for `new_table` (same name, new schema) without ever risking data
        loss or a missing table if `populate` fails: the current table is renamed aside first and
        only dropped after `new_table` has been created and populated successfully. Must be
        called from within an already-open transaction (`conn`); the caller's except block is
        responsible for calling `_restore_table_backup(name)` to recover if this raises.

        Fixes two real incidents found via a coverage-driven test pass (see llm-wiki/11-*.md):
        the previous DROP-then-recreate approach permanently lost the old rows if the reinsert
        failed after the DROP (SQLite's DDL here isn't rolled back by the enclosing transaction,
        contrary to what the code's own comment used to claim), and left the table entirely
        missing if table.create() itself failed after the DROP."""
        backup_name = f"{name}__migration_backup"
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {backup_name}")
        conn.exec_driver_sql(f"ALTER TABLE {name} RENAME TO {backup_name}")
        new_table.create(conn)
        populate(conn)
        conn.exec_driver_sql(f"DROP TABLE {backup_name}")

    def _restore_table_backup(self, name: str) -> None:
        """Recovery counterpart to _replace_table_safely: if that raised (or the process crashed
        mid-migration, before a previous run's except block could even attempt this), this
        restores the pre-migration table from its rename-aside backup -- dropping whatever
        partial new table may have been left behind -- so a failed/interrupted migration never
        leaves `name` missing or its old data unrecoverable; the migration simply retries on the
        next startup. A no-op if there's no backup to restore from."""
        backup_name = f"{name}__migration_backup"
        try:
            with self.engine.begin() as conn:
                insp = inspect(conn)
                if not insp.has_table(backup_name):
                    return
                if insp.has_table(name):
                    conn.exec_driver_sql(f"DROP TABLE {name}")
                conn.exec_driver_sql(f"ALTER TABLE {backup_name} RENAME TO {name}")
                logger.info(f"Restored {name} from its pre-migration backup after a failed/interrupted migration.")
        except Exception as restore_err:
            logger.error(f"Failed to restore {name} from its migration backup -- manual recovery needed: {restore_err}")

    def _migrate_to_owner_scoped_schema(self) -> None:
        """One-time, idempotent migration from the old vault_id-only schema to the
        (owner_username, vault_id) one. Safe to run on every startup: each step first checks
        whether it's already done. Reads the old vault_owner table (if present) to know who owned
        each vault_id, since under the old schema that mapping was unambiguous (vault_id really was
        globally unique so far, which is exactly the bug being fixed)."""
        inspector = inspect(self.engine)

        def vault_owner_map(conn) -> Dict[str, str]:
            if not inspector.has_table("vault_owner"):
                return {}
            try:
                rows = conn.exec_driver_sql("SELECT vault_id, owner_username FROM vault_owner").fetchall()
                return {r[0]: r[1] for r in rows}
            except Exception as e:
                logger.error(f"Could not read legacy vault_owner table during migration: {e}")
                return {}

        # file_history: owner_username was added as a plain nullable column above (its primary
        # key -- history_id -- never changed), so this only needs a backfill UPDATE, not a
        # drop/recreate.
        try:
            with self.engine.begin() as conn:
                needs_backfill = conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM file_history WHERE owner_username IS NULL OR owner_username = ''"
                ).scalar()
                if needs_backfill:
                    owners = vault_owner_map(conn)
                    for vault_id, owner in owners.items():
                        conn.execute(
                            update(self._file_history)
                            .where(self._file_history.c.vault_id == vault_id)
                            .values(owner_username=owner)
                        )
                    logger.info(f"Backfilled owner_username on file_history for {len(owners)} legacy vault(s).")
        except Exception as e:
            logger.error(f"file_history owner_username backfill failed (non-fatal): {e}")

        # file_metadata / published_files: owner_username joined the primary key, which most
        # dialects can't add to an existing table via a plain ALTER TABLE -- so these are read out
        # in full, then _replace_table_safely swaps in a freshly-created table (current schema)
        # populated with the transformed rows, without ever dropping the original until the
        # replacement is fully built (see that method's docstring for the incidents this avoids).
        for table, cols in (
            (self._file_metadata, ["vault_id", "path", "modified_at_ms", "size_bytes", "content_hash", "is_deleted"]),
            (self._published_files, ["vault_id", "path", "content_hash"]),
        ):
            name = table.name
            # Recovers from a hard crash between a previous attempt's rename-aside and its final
            # drop-the-backup step (the except block below can't run if the process died, not
            # just raised) -- a no-op if there's nothing to restore.
            self._restore_table_backup(name)
            inspector = inspect(self.engine)
            if "owner_username" in [c["name"] for c in inspector.get_columns(name)]:
                continue  # already migrated
            try:
                with self.engine.begin() as conn:
                    old_rows = conn.exec_driver_sql(f"SELECT {', '.join(cols)} FROM {name}").fetchall()
                    owners = vault_owner_map(conn)
                    transformed = [
                        {**dict(zip(cols, row)), "owner_username": owners.get(row[0], "")}
                        for row in old_rows
                    ]

                    def populate(c, table=table, transformed=transformed) -> None:
                        if transformed:
                            c.execute(insert(table), transformed)

                    self._replace_table_safely(conn, name, table, populate)
                    logger.info(f"Migrated {name} to the owner-scoped schema ({len(transformed)} row(s)).")
            except Exception as e:
                logger.error(f"Owner-scoping migration for {name} failed: {e}")
                self._restore_table_backup(name)

    def _migrate_plugin_enablement_to_vault_scoped_schema(self) -> None:
        """One-time, idempotent migration from the old (owner_username, plugin_id) primary key to
        the current (owner_username, vault_id, plugin_id) one. vault_id joined the primary key,
        so like file_metadata/published_files above, this can't be a plain ALTER TABLE -- but
        unlike those, there's no sibling table (vault_owner) that says which vault_id an old
        owner-scoped-only row was meant for: under the old schema, enabling a plugin for an owner
        applied to every vault that owner had, so there's no single correct vault_id to backfill
        with. Rather than guess, the (unrecoverable) old rows are dropped -- every vault that had
        a plugin enabled under the old schema just needs to re-enable it, this time genuinely
        scoped to the vault(s) that actually use it.
        """
        name = "plugin_enablement"
        # Recovers from a hard crash mid-migration on a previous startup -- a no-op otherwise.
        self._restore_table_backup(name)
        inspector = inspect(self.engine)
        if "vault_id" in [c["name"] for c in inspector.get_columns(name)]:
            return  # already migrated
        try:
            with self.engine.begin() as conn:
                # Old rows are intentionally discarded on a *successful* migration (see docstring
                # above) -- _replace_table_safely's rename-aside-then-drop only protects the
                # failure path (table.create() itself failing used to leave plugin_enablement
                # missing entirely; see llm-wiki/11-*.md), not the normal no-data-carried-over case.
                self._replace_table_safely(conn, name, self._plugin_enablement, lambda c: None)
                logger.info("Migrated plugin_enablement to the vault-scoped schema (old rows dropped -- re-enable per vault).")
        except Exception as e:
            logger.error(f"plugin_enablement vault-scoping migration failed: {e}")
            self._restore_table_backup(name)

    def _upsert(self, conn, table: Table, record: dict, pk_cols: list) -> None:
        pk_filter = [table.c[col] == record[col] for col in pk_cols]
        non_pk = {k: v for k, v in record.items() if k not in pk_cols}
        result = conn.execute(update(table).where(*pk_filter).values(**non_pk))
        if result.rowcount == 0:
            conn.execute(insert(table).values(**record))

    def _row_to_meta(self, row) -> Dict[str, Any]:
        m = dict(row._mapping)
        m["is_deleted"] = bool(m["is_deleted"])
        return m

    def load_all(self, owner_username: str, vault_id: str) -> Dict[str, Dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._file_metadata).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                )
            ).fetchall()
            return {r.path: self._row_to_meta(r) for r in rows}

    def load_one(self, owner_username: str, vault_id: str, path: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._file_metadata).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                    self._file_metadata.c.path == path,
                )
            ).fetchone()
            return self._row_to_meta(row) if row else None

    def save_all(self, owner_username: str, vault_id: str, files_meta: Dict[str, Dict[str, Any]]) -> None:
        with self.engine.begin() as conn:
            for path, meta in files_meta.items():
                self._upsert(conn, self._file_metadata, {
                    "owner_username": owner_username,
                    "vault_id": vault_id,
                    "path": path,
                    "modified_at_ms": meta["modified_at_ms"],
                    "size_bytes": meta["size_bytes"],
                    "content_hash": meta["content_hash"],
                    "is_deleted": bool(meta["is_deleted"]),
                }, ["owner_username", "vault_id", "path"])

    def save_one(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            self._upsert(conn, self._file_metadata, {
                "owner_username": owner_username,
                "vault_id": vault_id,
                "path": path,
                "modified_at_ms": meta["modified_at_ms"],
                "size_bytes": meta["size_bytes"],
                "content_hash": meta["content_hash"],
                "is_deleted": bool(meta["is_deleted"]),
            }, ["owner_username", "vault_id", "path"])

    def add_history(self, owner_username: str, vault_id: str, path: str, modified_at_ms: int,
                    size_bytes: int, content_hash: str, backup_file_path: str,
                    device_name: str, user_name: str,
                    deleted: bool = False, related_path: Optional[str] = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(self._file_history).values(
                owner_username=owner_username,
                vault_id=vault_id,
                path=path,
                modified_at_ms=modified_at_ms,
                size_bytes=size_bytes,
                content_hash=content_hash,
                backup_file_path=backup_file_path,
                device_name=device_name,
                user_name=user_name,
                deleted=deleted,
                related_path=related_path,
            ))

    def record_change(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(self._vault_change_log).values(
                owner_username=owner_username,
                vault_id=vault_id,
                path=path,
                content_hash=meta["content_hash"],
                size_bytes=meta["size_bytes"],
                modified_at_ms=meta["modified_at_ms"],
                is_deleted=bool(meta["is_deleted"]),
                recorded_at_ms=int(time.time() * 1000),
            ))

    def get_changes_since(self, owner_username: str, vault_id: str, since_change_id: int) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._vault_change_log).where(
                    self._vault_change_log.c.owner_username == owner_username,
                    self._vault_change_log.c.vault_id == vault_id,
                    self._vault_change_log.c.change_id > since_change_id,
                ).order_by(self._vault_change_log.c.change_id)
            ).fetchall()
            # Folds to each path's LATEST row -- rows are read in ascending change_id order, so
            # a later dict assignment for the same path naturally clobbers an earlier one.
            folded: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                folded[r.path] = {
                    "path": r.path,
                    "modified_at_ms": r.modified_at_ms,
                    "size_bytes": r.size_bytes,
                    "content_hash": r.content_hash,
                    "is_deleted": bool(r.is_deleted),
                }
            return list(folded.values())

    def get_latest_change_id(self, owner_username: str, vault_id: str) -> int:
        with self.engine.connect() as conn:
            result = conn.execute(
                select(func.max(self._vault_change_log.c.change_id)).where(
                    self._vault_change_log.c.owner_username == owner_username,
                    self._vault_change_log.c.vault_id == vault_id,
                )
            ).scalar()
            return int(result) if result is not None else 0

    def find_live_file_by_content_hash(self, owner_username: str, vault_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        if not content_hash:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._file_metadata).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                    self._file_metadata.c.content_hash == content_hash,
                    self._file_metadata.c.is_deleted.is_(False),
                )
            ).fetchone()
            return self._row_to_meta(row) if row else None

    @contextmanager
    def batch(self):
        with self.engine.begin() as conn:
            yield _SqlAlchemyBatch(self, conn)

    def migrate_history_on_rename(self, owner_username: str, vault_id: str, old_path: str, new_path: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(self._file_history)
                .where(
                    self._file_history.c.owner_username == owner_username,
                    self._file_history.c.vault_id == vault_id,
                    self._file_history.c.path == old_path
                )
                .values(path=new_path)
            )
            conn.execute(
                update(self._file_metadata)
                .where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                    self._file_metadata.c.path == old_path
                )
                .values(path=new_path)
            )

    def get_history(self, owner_username: str, vault_id: str, path: str) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._file_history)
                .where(
                    self._file_history.c.owner_username == owner_username,
                    self._file_history.c.vault_id == vault_id,
                    self._file_history.c.path == path,
                )
                .order_by(self._file_history.c.history_id.desc())
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_history_by_id(self, owner_username: str, history_id: int) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._file_history).where(
                    self._file_history.c.history_id == history_id,
                    self._file_history.c.owner_username == owner_username,
                )
            ).fetchone()
            return dict(row._mapping) if row else None

    def add_published_file(self, owner_username: str, vault_id: str, path: str, content_hash: str) -> None:
        with self.engine.begin() as conn:
            self._upsert(conn, self._published_files, {
                "owner_username": owner_username,
                "vault_id": vault_id,
                "path": path,
                "content_hash": content_hash,
            }, ["owner_username", "vault_id", "path"])

    def remove_published_file(self, owner_username: str, vault_id: str, path: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(self._published_files).where(
                    self._published_files.c.owner_username == owner_username,
                    self._published_files.c.vault_id == vault_id,
                    self._published_files.c.path == path,
                )
            )

    def get_published_files(self, owner_username: str, vault_id: str) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._published_files).where(
                    self._published_files.c.owner_username == owner_username,
                    self._published_files.c.vault_id == vault_id,
                )
            ).fetchall()
            return [{"path": r.path, "hash": r.content_hash} for r in rows]

    def enable_plugin(self, owner_username: str, vault_id: str, plugin_id: str, enabled_at_ms: int) -> None:
        with self.engine.begin() as conn:
            self._upsert(conn, self._plugin_enablement, {
                "owner_username": owner_username,
                "vault_id": vault_id,
                "plugin_id": plugin_id,
                "enabled_at_ms": enabled_at_ms,
            }, ["owner_username", "vault_id", "plugin_id"])

    def disable_plugin(self, owner_username: str, vault_id: str, plugin_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(self._plugin_enablement).where(
                    self._plugin_enablement.c.owner_username == owner_username,
                    self._plugin_enablement.c.vault_id == vault_id,
                    self._plugin_enablement.c.plugin_id == plugin_id,
                )
            )

    def list_enabled_plugins(self, owner_username: str, vault_id: str) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._plugin_enablement).where(
                    self._plugin_enablement.c.owner_username == owner_username,
                    self._plugin_enablement.c.vault_id == vault_id,
                )
            ).fetchall()
            return [{"plugin_id": r.plugin_id, "enabled_at_ms": r.enabled_at_ms} for r in rows]

    def list_all_enabled_plugins(self) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(select(self._plugin_enablement)).fetchall()
            return [
                {
                    "owner_username": r.owner_username,
                    "vault_id": r.vault_id,
                    "plugin_id": r.plugin_id,
                    "enabled_at_ms": r.enabled_at_ms,
                }
                for r in rows
            ]

    def create_user(self, username: str, password_hash: str, name: Optional[str] = None, email: Optional[str] = None, is_admin: bool = False) -> bool:
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self._users).where(self._users.c.username == username)
            ).fetchone()
            if existing:
                return False
            conn.execute(insert(self._users).values(
                username=username,
                password_hash=password_hash,
                name=name,
                email=email,
                is_admin=is_admin
            ))
            return True

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._users).where(self._users.c.username == username)
            ).fetchone()
            return dict(row._mapping) if row else None

    def create_device_token(self, token: str, username: str, device_name: str, created_at_ms: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(self._device_tokens).values(
                token=token,
                username=username,
                device_name=device_name,
                created_at_ms=created_at_ms,
            ))

    def get_device_token(self, token: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._device_tokens).where(self._device_tokens.c.token == token)
            ).fetchone()
            return dict(row._mapping) if row else None

    def list_device_tokens(self, username: str) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._device_tokens).where(self._device_tokens.c.username == username)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def delete_device_token(self, token: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(self._device_tokens).where(self._device_tokens.c.token == token))

    def create_oauth_client(self, client_id: str, client_name: str, redirect_uris: list, created_at_ms: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(self._oauth_clients).values(
                client_id=client_id,
                client_name=client_name,
                redirect_uris=json.dumps(redirect_uris),
                created_at_ms=created_at_ms,
            ))

    def get_oauth_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self._oauth_clients).where(self._oauth_clients.c.client_id == client_id)
            ).fetchone()
            if not row:
                return None
            data = dict(row._mapping)
            data["redirect_uris"] = json.loads(data["redirect_uris"])
            return data

    def set_plugin_config_entry(self, owner_username: str, vault_id: str, plugin_id: str, entry_id: str, config_blob_b64: str, updated_at_ms: int) -> None:
        with self.engine.begin() as conn:
            self._upsert(conn, self._plugin_config, {
                "owner_username": owner_username,
                "vault_id": vault_id,
                "plugin_id": plugin_id,
                "entry_id": entry_id,
                "config_blob": config_blob_b64,
                "updated_at_ms": updated_at_ms,
            }, ["owner_username", "vault_id", "plugin_id", "entry_id"])

    def get_plugin_config_entries(self, owner_username: str, vault_id: str, plugin_id: str) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._plugin_config).where(
                    self._plugin_config.c.owner_username == owner_username,
                    self._plugin_config.c.vault_id == vault_id,
                    self._plugin_config.c.plugin_id == plugin_id,
                )
            ).fetchall()
            return {
                r.entry_id: {"config_blob_b64": r.config_blob, "updated_at_ms": r.updated_at_ms}
                for r in rows
            }

    def delete_plugin_config_entry(self, owner_username: str, vault_id: str, plugin_id: str, entry_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(self._plugin_config).where(
                    self._plugin_config.c.owner_username == owner_username,
                    self._plugin_config.c.vault_id == vault_id,
                    self._plugin_config.c.plugin_id == plugin_id,
                    self._plugin_config.c.entry_id == entry_id,
                )
            )

    def get_vaults_by_owner(self, owner_username: str) -> list:
        with self.engine.connect() as conn:
            meta_ids = {r.vault_id for r in conn.execute(
                select(self._file_metadata.c.vault_id).distinct().where(
                    self._file_metadata.c.owner_username == owner_username
                )
            ).fetchall()}
            pub_ids = {r.vault_id for r in conn.execute(
                select(self._published_files.c.vault_id).distinct().where(
                    self._published_files.c.owner_username == owner_username
                )
            ).fetchall()}
            return sorted(meta_ids | pub_ids)

    def get_vault_size_bytes(self, owner_username: str, vault_id: str) -> int:
        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.sum(self._file_metadata.c.size_bytes)).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                    self._file_metadata.c.is_deleted.is_(False),
                )
            ).scalar()
            return int(total) if total is not None else 0

    def get_total_size_bytes(self, owner_username: str) -> int:
        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.sum(self._file_metadata.c.size_bytes)).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.is_deleted.is_(False),
                )
            ).scalar()
            return int(total) if total is not None else 0

    def purge_vault(self, owner_username: str, vault_id: str) -> list:
        """Deletes every file_metadata/file_history row for (owner, vault) and returns the
        removed history rows' backup_file_path list -- this class never touches the filesystem
        itself (see the rest of this file), so the caller (service.py, which already owns
        _backup_file/finalize_pushed_file's disk I/O) is responsible for actually deleting
        those backup files and the vault's own directory.

        Also records a tombstone in vault_change_log for every currently-live path before
        wiping file_metadata (#14_옵시디언싱크_정렬_구현계획.md's "PurgeVault와의 상호작용"): a
        client resuming from an old lastKnownChangeId must still learn every one of its known
        files is gone, not just find "nothing changed since then" because the rows it would have
        compared against no longer exist at all."""
        with self.engine.begin() as conn:
            backup_paths = [
                r.backup_file_path for r in conn.execute(
                    select(self._file_history.c.backup_file_path).where(
                        self._file_history.c.owner_username == owner_username,
                        self._file_history.c.vault_id == vault_id,
                    )
                ).fetchall()
            ]

            live_paths = conn.execute(
                select(self._file_metadata.c.path).where(
                    self._file_metadata.c.owner_username == owner_username,
                    self._file_metadata.c.vault_id == vault_id,
                    self._file_metadata.c.is_deleted.is_(False),
                )
            ).fetchall()
            now_ms = int(time.time() * 1000)
            for row in live_paths:
                conn.execute(insert(self._vault_change_log).values(
                    owner_username=owner_username,
                    vault_id=vault_id,
                    path=row.path,
                    content_hash="",
                    size_bytes=0,
                    modified_at_ms=now_ms,
                    is_deleted=True,
                    recorded_at_ms=now_ms,
                ))

            conn.execute(delete(self._file_history).where(
                self._file_history.c.owner_username == owner_username,
                self._file_history.c.vault_id == vault_id,
            ))
            conn.execute(delete(self._file_metadata).where(
                self._file_metadata.c.owner_username == owner_username,
                self._file_metadata.c.vault_id == vault_id,
            ))
            return backup_paths

    def get_all_users(self) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(select(self._users)).fetchall()
            return [
                {
                    "username": row.username,
                    "name": getattr(row, "name", None),
                    "email": getattr(row, "email", None),
                    "is_admin": bool(getattr(row, "is_admin", False))
                }
                for row in rows
            ]

    def delete_user(self, username: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(self._users).where(self._users.c.username == username))

    def update_user_password(self, username: str, password_hash: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(self._users).where(self._users.c.username == username).values(password_hash=password_hash)
            )

    def update_user_profile(self, username: str, name: str, email: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(self._users).where(self._users.c.username == username).values(name=name, email=email)
            )

    def add_vault_share(self, owner_username: str, vault_id: str, email: str) -> str:
        email = email.strip().lower()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self._vault_shares).where(
                    self._vault_shares.c.owner_username == owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                    self._vault_shares.c.email == email,
                )
            ).fetchone()
            if existing:
                return existing.uid

            share_uid = str(uuid.uuid4())
            conn.execute(insert(self._vault_shares).values(
                uid=share_uid,
                owner_username=owner_username,
                vault_id=vault_id,
                email=email,
                accepted=False,
                created_at_ms=int(time.time() * 1000),
            ))
            return share_uid

    def list_vault_shares(self, owner_username: str, vault_id: str) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self._vault_shares).where(
                    self._vault_shares.c.owner_username == owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                ).order_by(self._vault_shares.c.created_at_ms)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def remove_vault_share(self, owner_username: str, vault_id: str, share_uid: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(self._vault_shares).where(
                    self._vault_shares.c.owner_username == owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                    self._vault_shares.c.uid == share_uid,
                )
            )
            return result.rowcount > 0

    def remove_vault_share_by_invited_user(self, caller_username: str, owner_username: str, vault_id: str) -> bool:
        with self.engine.begin() as conn:
            caller = conn.execute(
                select(self._users.c.email).where(self._users.c.username == caller_username)
            ).fetchone()
            if not caller or not caller.email:
                return False

            result = conn.execute(
                delete(self._vault_shares).where(
                    self._vault_shares.c.owner_username == owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                    self._vault_shares.c.email == caller.email.strip().lower(),
                )
            )
            return result.rowcount > 0

    def list_shared_vaults_for_user(self, username: str) -> list:
        with self.engine.connect() as conn:
            user_row = conn.execute(
                select(self._users.c.email).where(self._users.c.username == username)
            ).fetchone()
            if not user_row or not user_row.email:
                return []

            rows = conn.execute(
                select(self._vault_shares).where(
                    self._vault_shares.c.email == user_row.email.strip().lower(),
                ).order_by(self._vault_shares.c.created_at_ms)
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    def get_accepted_share_usernames(self, owner_username: str, vault_id: str) -> list:
        with self.engine.connect() as conn:
            share_emails = conn.execute(
                select(self._vault_shares.c.email).where(
                    self._vault_shares.c.owner_username == owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                    self._vault_shares.c.accepted.is_(True),
                )
            ).fetchall()
            usernames = []
            for row in share_emails:
                user_row = conn.execute(
                    select(self._users.c.username).where(func.lower(self._users.c.email) == row.email)
                ).fetchone()
                if user_row:
                    usernames.append(user_row.username)
            return usernames

    def resolve_vault_owner(self, caller_username: str, requested_owner_username: str, vault_id: str,
                             mark_accepted: bool = False) -> str:
        # Self-owned short circuit: today's exact behavior, zero extra queries. Every existing
        # (non-shared) call site keeps working byte-for-byte identically through this branch.
        if not requested_owner_username or requested_owner_username == caller_username:
            return caller_username

        with self.engine.begin() as conn:
            caller = conn.execute(
                select(self._users.c.email).where(self._users.c.username == caller_username)
            ).fetchone()
            if not caller or not caller.email:
                raise VaultAccessDenied(f"{caller_username} has no email on file")

            caller_email = caller.email.strip().lower()
            share = conn.execute(
                select(self._vault_shares).where(
                    self._vault_shares.c.owner_username == requested_owner_username,
                    self._vault_shares.c.vault_id == vault_id,
                    self._vault_shares.c.email == caller_email,
                )
            ).fetchone()
            if not share:
                raise VaultAccessDenied(
                    f"{caller_username} has no share for {requested_owner_username}/{vault_id}"
                )

            # Binary access, no role gate: a still-pending share already grants full access
            # (matches real Obsidian Sync -- accepted is just an owner-facing status label, not
            # a permission check). mark_accepted=True flips it exactly once, from the one call
            # site (SyncWsSession._handle_init) where a live sync connection is the strongest
            # signal of genuine access-taking.
            if mark_accepted and not share.accepted:
                conn.execute(
                    update(self._vault_shares).where(self._vault_shares.c.id == share.id).values(
                        accepted=True, accepted_at_ms=int(time.time() * 1000),
                    )
                )

        return requested_owner_username


class _SqlAlchemyBatch:
    """Groups save_one/add_history calls onto one already-open connection/transaction
    so the caller pays for a single commit (fsync) instead of one per call."""

    def __init__(self, repo: "SqlAlchemyMetadataRepository", conn):
        self._repo = repo
        self._conn = conn

    def save_one(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        self._repo._upsert(self._conn, self._repo._file_metadata, {
            "owner_username": owner_username,
            "vault_id": vault_id,
            "path": path,
            "modified_at_ms": meta["modified_at_ms"],
            "size_bytes": meta["size_bytes"],
            "content_hash": meta["content_hash"],
            "is_deleted": bool(meta["is_deleted"]),
        }, ["owner_username", "vault_id", "path"])

    def add_history(self, owner_username: str, vault_id: str, path: str, modified_at_ms: int,
                    size_bytes: int, content_hash: str, backup_file_path: str,
                    device_name: str, user_name: str,
                    deleted: bool = False, related_path: Optional[str] = None) -> None:
        self._conn.execute(insert(self._repo._file_history).values(
            owner_username=owner_username,
            vault_id=vault_id,
            path=path,
            modified_at_ms=modified_at_ms,
            size_bytes=size_bytes,
            content_hash=content_hash,
            backup_file_path=backup_file_path,
            device_name=device_name,
            user_name=user_name,
            deleted=deleted,
            related_path=related_path,
        ))

    def record_change(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        self._conn.execute(insert(self._repo._vault_change_log).values(
            owner_username=owner_username,
            vault_id=vault_id,
            path=path,
            content_hash=meta["content_hash"],
            size_bytes=meta["size_bytes"],
            modified_at_ms=meta["modified_at_ms"],
            is_deleted=bool(meta["is_deleted"]),
            recorded_at_ms=int(time.time() * 1000),
        ))

