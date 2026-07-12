from abc import ABC, abstractmethod
from contextlib import contextmanager
import os
import json
import logging
from typing import Dict, Any, Optional

from sqlalchemy import (
    create_engine, MetaData, Table, Column, Index, inspect,
    BigInteger, Integer, String, Boolean, Text,
    select, insert, update, delete,
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
    def batch(self):
        """Context manager yielding an object with save_one/add_history that share a
        single commit, instead of each call committing (and fsyncing) on its own."""
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
    def get_vaults_by_owner(self, owner_username: str) -> list:
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
        # in full, the table is dropped and recreated from the current (correct) Table definition,
        # and the transformed rows are written back in. The in-memory copy is held until the
        # reinsert succeeds, so a failure after the DROP still logs exactly what needs restoring
        # from a backup.
        for table, cols in (
            (self._file_metadata, ["vault_id", "path", "modified_at_ms", "size_bytes", "content_hash", "is_deleted"]),
            (self._published_files, ["vault_id", "path", "content_hash"]),
        ):
            name = table.name
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
                    conn.exec_driver_sql(f"DROP TABLE {name}")
                    table.create(conn)
                    if transformed:
                        conn.execute(insert(table), transformed)
                    logger.info(f"Migrated {name} to the owner-scoped schema ({len(transformed)} row(s)).")
            except Exception as e:
                logger.error(
                    f"Owner-scoping migration for {name} failed -- table may be in an inconsistent "
                    f"state, restore from backup if so: {e}"
                )

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


class JsonFileMetadataRepository(MetadataRepository):
    """Flat-file fallback backend (no external DB server needed). Note: this backend's on-disk
    layout was never deployed with real multi-user data as far as we know (the only real
    deployment uses the SQL/CUBRID backend), so unlike SqlAlchemyMetadataRepository this doesn't
    carry an automatic migration off the old vault_id-only layout -- it just implements the
    owner-scoped layout directly."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.metadata_dir = os.path.join(data_dir, "metadata")
        os.makedirs(self.metadata_dir, exist_ok=True)
        self.users_file = os.path.join(data_dir, "users.json")

    def _load_users(self) -> dict:
        if os.path.exists(self.users_file):
            try:
                with open(self.users_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_users(self, users: dict) -> None:
        try:
            with open(self.users_file, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save users json: {e}")

    def init_db(self) -> None:
        pass

    def _owner_dir(self, owner_username: str) -> str:
        path = os.path.join(self.metadata_dir, owner_username)
        os.makedirs(path, exist_ok=True)
        return path

    def _get_path(self, owner_username: str, vault_id: str) -> str:
        return os.path.join(self._owner_dir(owner_username), f"{vault_id}.json")

    def load_all(self, owner_username: str, vault_id: str) -> Dict[str, Dict[str, Any]]:
        path = self._get_path(owner_username, vault_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f).get("files", {})
            except Exception as e:
                logger.error(f"Failed to load json metadata for {owner_username}/{vault_id}: {e}")
        return {}

    def load_one(self, owner_username: str, vault_id: str, path: str) -> Optional[Dict[str, Any]]:
        return self.load_all(owner_username, vault_id).get(path)

    def save_all(self, owner_username: str, vault_id: str, files_meta: Dict[str, Dict[str, Any]]) -> None:
        path = self._get_path(owner_username, vault_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"files": files_meta}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save json metadata for {owner_username}/{vault_id}: {e}")

    def save_one(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        files_meta = self.load_all(owner_username, vault_id)
        files_meta[path] = meta
        self.save_all(owner_username, vault_id, files_meta)

    def _get_history_path(self, owner_username: str, vault_id: str) -> str:
        return os.path.join(self._owner_dir(owner_username), f"{vault_id}_history.json")

    def _load_history_all(self, owner_username: str, vault_id: str) -> list:
        path = self._get_history_path(owner_username, vault_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load json history for {owner_username}/{vault_id}: {e}")
        return []

    def _save_history_all(self, owner_username: str, vault_id: str, history: list) -> None:
        path = self._get_history_path(owner_username, vault_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save json history for {owner_username}/{vault_id}: {e}")

    def add_history(self, owner_username: str, vault_id: str, path: str, modified_at_ms: int,
                    size_bytes: int, content_hash: str, backup_file_path: str,
                    device_name: str, user_name: str,
                    deleted: bool = False, related_path: Optional[str] = None) -> None:
        history = self._load_history_all(owner_username, vault_id)
        new_id = 1 if not history else max(h["history_id"] for h in history) + 1
        history.append({
            "history_id": new_id,
            "owner_username": owner_username,
            "vault_id": vault_id,
            "path": path,
            "modified_at_ms": modified_at_ms,
            "size_bytes": size_bytes,
            "content_hash": content_hash,
            "backup_file_path": backup_file_path,
            "device_name": device_name,
            "user_name": user_name,
            "deleted": deleted,
            "related_path": related_path,
        })
        self._save_history_all(owner_username, vault_id, history)

    @contextmanager
    def batch(self):
        b = _JsonBatch(self)
        yield b
        b.flush()

    def get_history(self, owner_username: str, vault_id: str, path: str) -> list:
        history = self._load_history_all(owner_username, vault_id)
        return sorted([self._with_history_defaults(h) for h in history if h["path"] == path],
                      key=lambda x: x["history_id"], reverse=True)

    def get_history_by_id(self, owner_username: str, history_id: int) -> Optional[dict]:
        owner_dir = self._owner_dir(owner_username)
        for file in os.listdir(owner_dir):
            if file.endswith("_history.json"):
                v_id = file[:-13]
                for h in self._load_history_all(owner_username, v_id):
                    if h["history_id"] == history_id:
                        return self._with_history_defaults(h)
        return None

    def _with_history_defaults(self, h: dict) -> dict:
        # Backfills defaults for backward compatibility with history entries saved before these fields existed
        h.setdefault("deleted", False)
        h.setdefault("related_path", None)
        return h

    def _get_publish_path(self, owner_username: str, vault_id: str) -> str:
        return os.path.join(self._owner_dir(owner_username), f"{vault_id}_publish.json")

    def _load_publish_all(self, owner_username: str, vault_id: str) -> list:
        path = self._get_publish_path(owner_username, vault_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load json publish for {owner_username}/{vault_id}: {e}")
        return []

    def _save_publish_all(self, owner_username: str, vault_id: str, publish_list: list) -> None:
        path = self._get_publish_path(owner_username, vault_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(publish_list, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save json publish for {owner_username}/{vault_id}: {e}")

    def add_published_file(self, owner_username: str, vault_id: str, path: str, content_hash: str) -> None:
        publish_list = self._load_publish_all(owner_username, vault_id)
        for p in publish_list:
            if p["path"] == path:
                p["hash"] = content_hash
                break
        else:
            publish_list.append({"path": path, "hash": content_hash})
        self._save_publish_all(owner_username, vault_id, publish_list)

    def remove_published_file(self, owner_username: str, vault_id: str, path: str) -> None:
        publish_list = [p for p in self._load_publish_all(owner_username, vault_id) if p["path"] != path]
        self._save_publish_all(owner_username, vault_id, publish_list)

    def get_published_files(self, owner_username: str, vault_id: str) -> list:
        return self._load_publish_all(owner_username, vault_id)

    def migrate_history_on_rename(self, owner_username: str, vault_id: str, old_path: str, new_path: str) -> None:
        # 1. Update the path in the file_history JSON file
        history = self._load_history_all(owner_username, vault_id)
        updated = False
        for h in history:
            if h["path"] == old_path:
                h["path"] = new_path
                updated = True
        if updated:
            self._save_history_all(owner_username, vault_id, history)

        # 2. Remove the old_path entry from the metadata JSON file
        metadata = self.load_all(owner_username, vault_id)
        if old_path in metadata:
            del metadata[old_path]
            self.save_all(owner_username, vault_id, metadata)

    def create_user(self, username: str, password_hash: str, name: Optional[str] = None, email: Optional[str] = None, is_admin: bool = False) -> bool:
        users = self._load_users()
        if username in users:
            return False
        users[username] = {
            "username": username,
            "password_hash": password_hash,
            "name": name,
            "email": email,
            "is_admin": is_admin
        }
        self._save_users(users)
        return True

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        users = self._load_users()
        return users.get(username)

    def _get_device_tokens_path(self) -> str:
        return os.path.join(self.metadata_dir, "device_tokens.json")

    def _load_device_tokens(self) -> dict:
        path = self._get_device_tokens_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load device tokens: {e}")
        return {}

    def _save_device_tokens(self, tokens: dict) -> None:
        path = self._get_device_tokens_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save device tokens: {e}")

    def create_device_token(self, token: str, username: str, device_name: str, created_at_ms: int) -> None:
        tokens = self._load_device_tokens()
        tokens[token] = {"username": username, "device_name": device_name, "created_at_ms": created_at_ms}
        self._save_device_tokens(tokens)

    def get_device_token(self, token: str) -> Optional[Dict[str, Any]]:
        entry = self._load_device_tokens().get(token)
        return {"token": token, **entry} if entry else None

    def list_device_tokens(self, username: str) -> list:
        tokens = self._load_device_tokens()
        return [{"token": t, **e} for t, e in tokens.items() if e.get("username") == username]

    def delete_device_token(self, token: str) -> None:
        tokens = self._load_device_tokens()
        if token in tokens:
            del tokens[token]
            self._save_device_tokens(tokens)

    def get_vaults_by_owner(self, owner_username: str) -> list:
        owner_dir = self._owner_dir(owner_username)
        vault_ids = set()
        for file in os.listdir(owner_dir):
            if file.endswith("_history.json"):
                vault_ids.add(file[:-13])
            elif file.endswith("_publish.json"):
                vault_ids.add(file[:-13])
            elif file.endswith(".json"):
                vault_ids.add(file[:-5])
        return sorted(vault_ids)

    def get_all_users(self) -> list:
        users = self._load_users()
        return [
            {
                "username": u["username"],
                "name": u.get("name"),
                "email": u.get("email"),
                "is_admin": bool(u.get("is_admin", False))
            }
            for u in users.values()
        ]

    def delete_user(self, username: str) -> None:
        users = self._load_users()
        if username in users:
            del users[username]
            self._save_users(users)

    def update_user_password(self, username: str, password_hash: str) -> None:
        users = self._load_users()
        if username in users:
            users[username]["password_hash"] = password_hash
            self._save_users(users)

    def update_user_profile(self, username: str, name: str, email: str) -> None:
        users = self._load_users()
        if username in users:
            users[username]["name"] = name
            users[username]["email"] = email
            self._save_users(users)


class _JsonBatch:
    """Groups save_one/add_history calls in memory and writes each touched vault's
    metadata/history JSON file once on flush, instead of once per call."""

    def __init__(self, repo: "JsonFileMetadataRepository"):
        self._repo = repo
        self._metadata: Dict[tuple, Dict[str, Any]] = {}
        self._history: Dict[tuple, list] = {}

    def save_one(self, owner_username: str, vault_id: str, path: str, meta: Dict[str, Any]) -> None:
        key = (owner_username, vault_id)
        if key not in self._metadata:
            self._metadata[key] = self._repo.load_all(owner_username, vault_id)
        self._metadata[key][path] = meta

    def add_history(self, owner_username: str, vault_id: str, path: str, modified_at_ms: int,
                    size_bytes: int, content_hash: str, backup_file_path: str,
                    device_name: str, user_name: str,
                    deleted: bool = False, related_path: Optional[str] = None) -> None:
        key = (owner_username, vault_id)
        if key not in self._history:
            self._history[key] = self._repo._load_history_all(owner_username, vault_id)
        history = self._history[key]
        new_id = 1 if not history else max(h["history_id"] for h in history) + 1
        history.append({
            "history_id": new_id,
            "owner_username": owner_username,
            "vault_id": vault_id,
            "path": path,
            "modified_at_ms": modified_at_ms,
            "size_bytes": size_bytes,
            "content_hash": content_hash,
            "backup_file_path": backup_file_path,
            "device_name": device_name,
            "user_name": user_name,
            "deleted": deleted,
            "related_path": related_path,
        })

    def flush(self) -> None:
        for (owner_username, vault_id), files_meta in self._metadata.items():
            self._repo.save_all(owner_username, vault_id, files_meta)
        for (owner_username, vault_id), history in self._history.items():
            self._repo._save_history_all(owner_username, vault_id, history)
