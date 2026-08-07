"""TDD for the MCP tool handlers (5절 of MCP_STREAMABLE_HTTP_구현_계획.md) -- pure functions,
independent of the mcp SDK and of Twisted, so they're tested directly against a minimal in-memory
fake repository rather than a real database or a real MCP client round-trip (10절's "순수 로직"
tier). The McpResource/SDK wiring (session handling, JSON-RPC framing) is integration-tested
separately/manually, same split established elsewhere in this codebase (e.g. history_policy.py vs
service.py).
"""

import base64

import pytest

from pumice_server.e2ee import decrypt, derive_key, encrypt
from pumice_server.mcp_tools import (
    ToolError,
    delete_note,
    get_file_history,
    get_recent_notes,
    list_files,
    list_published_files,
    list_vaults,
    read_file_version,
    read_note,
    rename_note,
    resolve_vault_file_path,
    search_by_frontmatter,
    search_notes,
    write_note,
)


class FakeRepository:
    """Implements just the MetadataRepository methods the MCP tools call, in-memory."""

    def __init__(self):
        self._vaults_by_owner = {}
        self._files = {}  # (owner, vault_id) -> {path: meta}
        self._history = {}  # (owner, vault_id, path) -> [entry, ...], newest first
        self._history_by_id = {}  # history_id -> entry
        self._published = {}  # (owner, vault_id) -> [{"path", "hash"}]
        self.add_history_calls = []
        self._next_history_id = 1000

    def get_vaults_by_owner(self, owner_username):
        return sorted(self._vaults_by_owner.get(owner_username, set()))

    def load_all(self, owner_username, vault_id):
        return dict(self._files.get((owner_username, vault_id), {}))

    def load_one(self, owner_username, vault_id, path):
        return self._files.get((owner_username, vault_id), {}).get(path)

    def save_one(self, owner_username, vault_id, path, meta):
        self._vaults_by_owner.setdefault(owner_username, set()).add(vault_id)
        self._files.setdefault((owner_username, vault_id), {})[path] = dict(meta)

    def add_history(self, **kwargs):
        self.add_history_calls.append(kwargs)
        # Also feed get_history/get_history_by_id, like the real add_history's INSERT does --
        # otherwise tests asserting on post-add_history read-back (e.g. via rename's history
        # migration) would see stale state.
        history_id = self._next_history_id
        self._next_history_id += 1
        full = {
            "history_id": history_id,
            "owner_username": kwargs["owner_username"],
            "vault_id": kwargs["vault_id"],
            "path": kwargs["path"],
            "modified_at_ms": kwargs["modified_at_ms"],
            "size_bytes": kwargs["size_bytes"],
            "content_hash": kwargs["content_hash"],
            "backup_file_path": kwargs["backup_file_path"],
            "device_name": kwargs["device_name"],
            "user_name": kwargs["user_name"],
            "deleted": kwargs.get("deleted", False),
            "related_path": kwargs.get("related_path"),
        }
        key = (full["owner_username"], full["vault_id"], full["path"])
        self._history.setdefault(key, []).insert(0, full)
        self._history_by_id[history_id] = full

    def migrate_history_on_rename(self, owner_username, vault_id, old_path, new_path):
        # Real behavior (repository.py): an in-place UPDATE relabeling the path column of both
        # the metadata row and every history row -- not a copy, and not a tombstone left behind
        # at old_path.
        files = self._files.get((owner_username, vault_id), {})
        if old_path in files:
            files[new_path] = files.pop(old_path)
        key_old = (owner_username, vault_id, old_path)
        key_new = (owner_username, vault_id, new_path)
        if key_old in self._history:
            self._history[key_new] = self._history.pop(key_old)
            for entry in self._history[key_new]:
                entry["path"] = new_path
        for entry in self._history_by_id.values():
            if entry["owner_username"] == owner_username and entry["vault_id"] == vault_id and entry["path"] == old_path:
                entry["path"] = new_path

    def get_history(self, owner_username, vault_id, path):
        return list(self._history.get((owner_username, vault_id, path), []))

    def get_history_by_id(self, owner_username, history_id):
        entry = self._history_by_id.get(history_id)
        if entry is None or entry["owner_username"] != owner_username:
            return None
        return dict(entry)

    def get_published_files(self, owner_username, vault_id):
        return list(self._published.get((owner_username, vault_id), []))

    # --- test-only helpers, not part of MetadataRepository ---

    def seed_file(self, owner, vault_id, path, **meta):
        self._vaults_by_owner.setdefault(owner, set()).add(vault_id)
        self._files.setdefault((owner, vault_id), {})[path] = {
            "path": path,
            "modified_at_ms": 0,
            "size_bytes": 0,
            "content_hash": "",
            "is_deleted": False,
            **meta,
        }

    def seed_history(self, history_id, owner, vault_id, path, **entry):
        full = {
            "history_id": history_id,
            "owner_username": owner,
            "vault_id": vault_id,
            "path": path,
            "modified_at_ms": 0,
            "size_bytes": 0,
            "content_hash": "",
            "backup_file_path": f"/fake/{history_id}.bak",
            "device_name": "",
            "user_name": "",
            "deleted": False,
            "related_path": None,
            **entry,
        }
        self._history.setdefault((owner, vault_id, path), []).insert(0, full)
        self._history_by_id[history_id] = full

    def seed_published(self, owner, vault_id, path, content_hash):
        self._published.setdefault((owner, vault_id), []).append({"path": path, "hash": content_hash})


def test_list_vaults_returns_owners_vaults():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    repo.seed_file("alice", "vault2", "b.md")
    repo.seed_file("bob", "other-vault", "c.md")

    result = list_vaults(repo, "alice")

    assert result == {"vaults": ["vault1", "vault2"]}


def test_list_files_excludes_deleted_entries():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", size_bytes=10, content_hash="h1")
    repo.seed_file("alice", "vault1", "deleted.md", is_deleted=True)

    result = list_files(repo, "alice", "vault1")

    paths = [f["path"] for f in result["files"]]
    assert paths == ["a.md"]
    assert result["files"][0]["size_bytes"] == 10
    assert result["files"][0]["content_hash"] == "h1"


def test_list_files_does_not_leak_owner_or_vault_columns():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = list_files(repo, "alice", "vault1")

    assert "owner_username" not in result["files"][0]
    assert "vault_id" not in result["files"][0]


def test_get_file_history_returns_entries_without_internal_backup_path():
    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md", device_name="laptop")

    result = get_file_history(repo, "alice", "vault1", "a.md")

    assert len(result["history"]) == 1
    assert result["history"][0]["history_id"] == 1
    assert result["history"][0]["device_name"] == "laptop"
    assert "backup_file_path" not in result["history"][0]


def test_list_published_files_passthrough():
    repo = FakeRepository()
    repo.seed_published("alice", "vault1", "site/index.md", "hash123")

    result = list_published_files(repo, "alice", "vault1")

    assert result == {"published_files": [{"path": "site/index.md", "hash": "hash123"}]}


def test_read_file_version_returns_plaintext_when_not_encrypted():
    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md")

    result = read_file_version(repo, "alice", 1, read_bytes=lambda _path: b"hello world")

    assert result == {"path": "a.md", "content": "hello world", "encoding": "utf-8"}


def test_read_file_version_decrypts_with_correct_password():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    password = "correct horse"
    vault_id = "vault1"
    key = derive_key(password, vault_id)
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"secret note contents", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_history(1, "alice", vault_id, "a.md")

    result = read_file_version(repo, "alice", 1, e2ee_password=password, read_bytes=lambda _path: wire_blob)

    assert result == {"path": "a.md", "content": "secret note contents", "encoding": "utf-8"}


def test_read_file_version_raises_on_wrong_password():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key("correct horse", "vault1")
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"secret note contents", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md")

    with pytest.raises(ToolError):
        read_file_version(repo, "alice", 1, e2ee_password="wrong password", read_bytes=lambda _path: wire_blob)


def test_read_file_version_suggests_password_when_content_is_binary_and_none_given():
    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md")

    with pytest.raises(ToolError, match="e2ee_password"):
        read_file_version(repo, "alice", 1, read_bytes=lambda _path: b"\xff\xfe\x00\x01not-utf8")


def test_read_file_version_raises_for_unknown_history_id():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        read_file_version(repo, "alice", 999, read_bytes=lambda _path: b"")


def test_read_file_version_raises_for_another_owners_history_id():
    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md")
    with pytest.raises(ToolError):
        read_file_version(repo, "bob", 1, read_bytes=lambda _path: b"")


def test_read_file_version_raises_for_deletion_tombstone():
    repo = FakeRepository()
    repo.seed_history(1, "alice", "vault1", "a.md", deleted=True)
    with pytest.raises(ToolError):
        read_file_version(repo, "alice", 1, read_bytes=lambda _path: b"")


def test_read_file_version_base64_encodes_binary_content_when_password_provided_but_result_not_utf8():
    # Password provided and correct, but the decrypted plaintext itself happens to be binary
    # (e.g. an image) -- should base64 it rather than raise, since a password WAS supplied.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    password = "pw"
    vault_id = "vault1"
    key = derive_key(password, vault_id)
    iv = b"\x00" * 12
    binary_plaintext = b"\xff\xd8\xff\xe0binary-image-bytes"
    ciphertext_and_tag = AESGCM(key).encrypt(iv, binary_plaintext, None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_history(1, "alice", vault_id, "img.png")

    result = read_file_version(repo, "alice", 1, e2ee_password=password, read_bytes=lambda _path: wire_blob)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == binary_plaintext


def test_get_recent_notes_sorted_newest_first():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "old.md", modified_at_ms=100)
    repo.seed_file("alice", "vault1", "new.md", modified_at_ms=300)
    repo.seed_file("alice", "vault1", "mid.md", modified_at_ms=200)

    result = get_recent_notes(repo, "alice", "vault1")

    assert [f["path"] for f in result["files"]] == ["new.md", "mid.md", "old.md"]


def test_get_recent_notes_excludes_deleted():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", modified_at_ms=100)
    repo.seed_file("alice", "vault1", "gone.md", modified_at_ms=200, is_deleted=True)

    result = get_recent_notes(repo, "alice", "vault1")

    assert [f["path"] for f in result["files"]] == ["a.md"]


def test_get_recent_notes_respects_limit():
    repo = FakeRepository()
    for i in range(5):
        repo.seed_file("alice", "vault1", f"{i}.md", modified_at_ms=i)

    result = get_recent_notes(repo, "alice", "vault1", limit=2)

    assert len(result["files"]) == 2
    assert [f["path"] for f in result["files"]] == ["4.md", "3.md"]


def test_get_recent_notes_does_not_leak_owner_or_vault_columns():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = get_recent_notes(repo, "alice", "vault1")

    assert "owner_username" not in result["files"][0]
    assert "vault_id" not in result["files"][0]


def test_search_notes_finds_matching_file():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    repo.seed_file("alice", "vault1", "b.md")

    def read_file(_owner, _vault_id, path):
        return {"a.md": b"the quick brown fox", "b.md": b"nothing relevant here"}[path]

    result = search_notes(repo, "alice", "vault1", "quick brown", read_file=read_file)

    assert [m["path"] for m in result["matches"]] == ["a.md"]
    assert "quick brown" in result["matches"][0]["snippet"]


def test_search_notes_is_case_insensitive():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_notes(
        repo, "alice", "vault1", "QUICK", read_file=lambda *_: b"the quick brown fox"
    )

    assert [m["path"] for m in result["matches"]] == ["a.md"]


def test_search_notes_skips_deleted_files():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone.md", is_deleted=True)

    result = search_notes(repo, "alice", "vault1", "anything", read_file=lambda *_: b"anything here")

    assert result["matches"] == []


def test_search_notes_skips_binary_undecodable_content():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "image.png")

    result = search_notes(
        repo, "alice", "vault1", "anything", read_file=lambda *_: b"\xff\xfe\x00\x01not-utf8"
    )

    assert result["matches"] == []


def test_search_notes_returns_empty_when_no_matches():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_notes(repo, "alice", "vault1", "needle", read_file=lambda *_: b"haystack haystack")

    assert result == {"matches": [], "truncated": False}


def test_search_notes_decrypts_when_correct_password_given():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    password = "correct horse"
    vault_id = "vault1"
    key = derive_key(password, vault_id)
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"the secret ingredient is love", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", vault_id, "a.md")

    result = search_notes(
        repo, "alice", vault_id, "secret ingredient", e2ee_password=password, read_file=lambda *_: wire_blob
    )

    assert [m["path"] for m in result["matches"]] == ["a.md"]


def test_search_notes_skips_file_when_password_wrong():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key("correct horse", "vault1")
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"the secret ingredient is love", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_notes(
        repo, "alice", "vault1", "secret", e2ee_password="wrong password", read_file=lambda *_: wire_blob
    )

    assert result["matches"] == []


def test_search_notes_respects_max_results_and_reports_truncation():
    repo = FakeRepository()
    for i in range(5):
        repo.seed_file("alice", "vault1", f"{i}.md")

    result = search_notes(
        repo, "alice", "vault1", "needle", read_file=lambda *_: b"needle here", max_results=2
    )

    assert len(result["matches"]) == 2
    assert result["truncated"] is True


def test_search_notes_skips_files_that_fail_to_read():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone-on-disk.md")

    def read_file(_owner, _vault_id, _path):
        raise FileNotFoundError()

    result = search_notes(repo, "alice", "vault1", "anything", read_file=read_file)

    assert result["matches"] == []


def test_resolve_vault_file_path_returns_path_inside_vault_dir(tmp_path):
    data_dir = str(tmp_path)
    resolved = resolve_vault_file_path(data_dir, "alice", "vault1", "notes/a.md")
    assert resolved == str(tmp_path / "vaults" / "alice" / "vault1" / "notes" / "a.md")


def test_resolve_vault_file_path_rejects_parent_traversal(tmp_path):
    data_dir = str(tmp_path)
    with pytest.raises(ToolError):
        resolve_vault_file_path(data_dir, "alice", "vault1", "../../../etc/passwd")


def test_read_note_returns_current_content():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = read_note(repo, "alice", "vault1", "a.md", read_file=lambda *_: b"hello current world")

    assert result == {"path": "a.md", "content": "hello current world", "encoding": "utf-8"}


def test_read_note_raises_for_missing_note():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        read_note(repo, "alice", "vault1", "nope.md", read_file=lambda *_: b"")


def test_read_note_raises_for_deleted_note():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone.md", is_deleted=True)
    with pytest.raises(ToolError):
        read_note(repo, "alice", "vault1", "gone.md", read_file=lambda *_: b"")


def test_read_note_raises_when_file_cannot_be_read():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    def read_file(*_args):
        raise FileNotFoundError("gone from disk")

    with pytest.raises(ToolError):
        read_note(repo, "alice", "vault1", "a.md", read_file=read_file)


def test_read_note_decrypts_with_correct_password():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    password = "correct horse"
    vault_id = "vault1"
    key = derive_key(password, vault_id)
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"secret current content", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", vault_id, "a.md")

    result = read_note(repo, "alice", vault_id, "a.md", read_file=lambda *_: wire_blob, e2ee_password=password)

    assert result == {"path": "a.md", "content": "secret current content", "encoding": "utf-8"}


def test_read_note_raises_on_wrong_password():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key("correct horse", "vault1")
    iv = b"\x00" * 12
    ciphertext_and_tag = AESGCM(key).encrypt(iv, b"secret current content", None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    with pytest.raises(ToolError):
        read_note(repo, "alice", "vault1", "a.md", read_file=lambda *_: wire_blob, e2ee_password="wrong")


def test_read_note_suggests_password_when_binary_and_none_given():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    with pytest.raises(ToolError, match="e2ee_password"):
        read_note(repo, "alice", "vault1", "a.md", read_file=lambda *_: b"\xff\xfe\x00\x01not-utf8")


def test_search_by_frontmatter_finds_matching_note():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: b"---\nstatus: done\n---\nbody text"
    )

    assert result == {"paths": ["a.md"], "truncated": False}


def test_search_by_frontmatter_no_match_when_value_differs():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: b"---\nstatus: in-progress\n---\nbody"
    )

    assert result == {"paths": [], "truncated": False}


def test_search_by_frontmatter_no_match_when_key_absent():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: b"---\ntags: urgent\n---\nbody"
    )

    assert result == {"paths": [], "truncated": False}


def test_search_by_frontmatter_supports_substring_match_for_list_style_values():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "tags", "urgent", read_file=lambda *_: b"---\ntags: [project, urgent]\n---\nbody"
    )

    assert result == {"paths": ["a.md"], "truncated": False}


def test_search_by_frontmatter_skips_notes_without_frontmatter():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: b"just a plain note, no frontmatter"
    )

    assert result == {"paths": [], "truncated": False}


def test_search_by_frontmatter_skips_deleted_notes():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone.md", is_deleted=True)

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: b"---\nstatus: done\n---\n"
    )

    assert result == {"paths": [], "truncated": False}


def test_search_by_frontmatter_decrypts_with_correct_password():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    password = "correct horse"
    vault_id = "vault1"
    key = derive_key(password, vault_id)
    iv = b"\x00" * 12
    plaintext = "---\nstatus: done\n---\nbody"
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", vault_id, "a.md")

    result = search_by_frontmatter(
        repo, "alice", vault_id, "status", "done", read_file=lambda *_: wire_blob, e2ee_password=password
    )

    assert result == {"paths": ["a.md"], "truncated": False}


def test_search_by_frontmatter_skips_file_when_password_wrong():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key("correct horse", "vault1")
    iv = b"\x00" * 12
    plaintext = "---\nstatus: done\n---\n"
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    wire_blob = iv + ciphertext_and_tag[-16:] + ciphertext_and_tag[:-16]

    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    result = search_by_frontmatter(
        repo, "alice", "vault1", "status", "done", read_file=lambda *_: wire_blob, e2ee_password="wrong"
    )

    assert result == {"paths": [], "truncated": False}


def test_search_by_frontmatter_respects_max_results_and_reports_truncation():
    repo = FakeRepository()
    for i in range(5):
        repo.seed_file("alice", "vault1", f"{i}.md")

    result = search_by_frontmatter(
        repo,
        "alice",
        "vault1",
        "status",
        "done",
        read_file=lambda *_: b"---\nstatus: done\n---\n",
        max_results=2,
    )

    assert len(result["paths"]) == 2
    assert result["truncated"] is True


def test_resolve_vault_file_path_rejects_sibling_vault_prefix_collision(tmp_path):
    # "vault1x" must not be treated as inside "vault1" just because it shares a string prefix --
    # a naive `.startswith()` boundary check (rather than a real path-component comparison) would
    # let this through.
    data_dir = str(tmp_path)
    (tmp_path / "vaults" / "alice" / "vault1x").mkdir(parents=True)
    with pytest.raises(ToolError):
        resolve_vault_file_path(data_dir, "alice", "vault1", "../vault1x/secret.md")


class FakeWriter:
    """Records what write_note actually wrote, and stands in for the production write_file
    callable (which does real disk I/O + backup-file creation and reports back the resulting
    modified_at_ms/size_bytes/backup_file_path -- see mcp_resource.py)."""

    def __init__(self, modified_at_ms=12345):
        self.written = {}  # (owner, vault_id, path) -> raw bytes
        self.modified_at_ms = modified_at_ms

    def __call__(self, owner_username, vault_id, path, raw):
        self.written[(owner_username, vault_id, path)] = raw
        return {"modified_at_ms": self.modified_at_ms, "size_bytes": len(raw), "backup_file_path": "/fake/new.bak"}


def test_write_note_writes_plaintext_to_new_file_in_empty_vault():
    repo = FakeRepository()
    writer = FakeWriter()

    result = write_note(repo, "alice", "vault1", "new.md", "hello world", read_file=lambda *_: b"", write_file=writer)

    assert writer.written[("alice", "vault1", "new.md")] == b"hello world"
    assert result == {"path": "new.md", "size_bytes": 11, "encrypted": False}


def test_write_note_writes_plaintext_when_existing_files_are_plaintext():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    writer = FakeWriter()

    write_note(
        repo, "alice", "vault1", "b.md", "new content", read_file=lambda *_: b"existing plaintext", write_file=writer
    )

    assert writer.written[("alice", "vault1", "b.md")] == b"new content"


def test_write_note_refuses_plaintext_write_when_existing_files_look_encrypted():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    writer = FakeWriter()

    with pytest.raises(ToolError, match="e2ee_password"):
        write_note(
            repo,
            "alice",
            "vault1",
            "b.md",
            "new content",
            read_file=lambda *_: b"\xff\xfe\x00\x01not-utf8",
            write_file=writer,
        )
    assert writer.written == {}


def test_write_note_writes_plaintext_when_only_some_sampled_files_look_encrypted():
    # A vault with ordinary binary attachments (images, PDFs, ...) sorted early among the
    # sampled paths must not be mistaken for an encrypted vault -- real E2EE encrypts every
    # file uniformly, so a real encrypted vault's sample should fail unanimously. A single
    # binary attachment mixed in with otherwise-plaintext notes is the normal, expected case,
    # not a signal of encryption.
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.jpg")  # binary attachment, sorted first
    repo.seed_file("alice", "vault1", "b.md")
    repo.seed_file("alice", "vault1", "c.md")
    writer = FakeWriter()

    def read_file(_owner, _vault_id, path):
        if path == "a.jpg":
            return b"\xff\xd8\xff\xe0binary-jpeg-bytes"
        return b"plain text content"

    write_note(repo, "alice", "vault1", "new.md", "hello", read_file=read_file, write_file=writer)

    assert writer.written[("alice", "vault1", "new.md")] == b"hello"


def test_write_note_refuses_when_all_sampled_files_look_encrypted():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    repo.seed_file("alice", "vault1", "b.md")
    writer = FakeWriter()

    with pytest.raises(ToolError, match="e2ee_password"):
        write_note(
            repo,
            "alice",
            "vault1",
            "c.md",
            "new content",
            read_file=lambda *_: b"\xff\xfe\x00\x01not-utf8",
            write_file=writer,
        )
    assert writer.written == {}


def test_write_note_ignores_deleted_files_when_checking_for_encryption():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone.md", is_deleted=True)
    writer = FakeWriter()

    # The only existing file "looks encrypted", but it's deleted -- shouldn't be sampled, so this
    # (otherwise-empty) vault is still treated as unencrypted.
    write_note(
        repo,
        "alice",
        "vault1",
        "new.md",
        "hello",
        read_file=lambda *_: b"\xff\xfe\x00\x01not-utf8",
        write_file=writer,
    )

    assert writer.written[("alice", "vault1", "new.md")] == b"hello"


def test_write_note_encrypts_when_password_given():
    repo = FakeRepository()
    writer = FakeWriter()
    password, vault_id = "correct horse", "vault1"

    result = write_note(
        repo, "alice", vault_id, "a.md", "secret content", read_file=lambda *_: b"", write_file=writer,
        e2ee_password=password,
    )

    key = derive_key(password, vault_id)
    expected_wire_blob = encrypt(b"secret content", key)
    assert writer.written[("alice", vault_id, "a.md")] == expected_wire_blob
    assert result["encrypted"] is True
    # And it must actually be decryptable by a real client with the same password afterward.
    assert decrypt(writer.written[("alice", vault_id, "a.md")], key) == b"secret content"


def test_write_note_encrypts_even_when_existing_files_look_like_plaintext():
    # Explicit e2ee_password always wins over the "existing files look unencrypted" inference --
    # e.g. the very first write to a fresh vault the caller already knows should be encrypted.
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    writer = FakeWriter()

    result = write_note(
        repo,
        "alice",
        "vault1",
        "b.md",
        "secret",
        read_file=lambda *_: b"totally plain text",
        write_file=writer,
        e2ee_password="pw",
    )

    assert result["encrypted"] is True
    assert writer.written[("alice", "vault1", "b.md")] != b"secret"


def test_write_note_updates_repository_metadata():
    repo = FakeRepository()
    writer = FakeWriter(modified_at_ms=999)

    write_note(repo, "alice", "vault1", "new.md", "hello world", read_file=lambda *_: b"", write_file=writer)

    meta = repo.load_one("alice", "vault1", "new.md")
    assert meta["path"] == "new.md"
    assert meta["modified_at_ms"] == 999
    assert meta["size_bytes"] == len(b"hello world")
    assert meta["is_deleted"] is False
    assert meta["content_hash"]  # non-empty


def test_write_note_records_a_history_entry():
    repo = FakeRepository()
    writer = FakeWriter(modified_at_ms=999)

    write_note(repo, "alice", "vault1", "new.md", "hello world", read_file=lambda *_: b"", write_file=writer)

    assert len(repo.add_history_calls) == 1
    entry = repo.add_history_calls[0]
    assert entry["owner_username"] == "alice"
    assert entry["vault_id"] == "vault1"
    assert entry["path"] == "new.md"
    assert entry["modified_at_ms"] == 999
    assert entry["backup_file_path"] == "/fake/new.bak"


def test_write_note_only_samples_first_few_existing_files():
    repo = FakeRepository()
    # 5 plaintext files, then one that looks encrypted -- default sample_size is small enough
    # that the encrypted-looking one (sorted last) is never even looked at.
    for i in range(5):
        repo.seed_file("alice", "vault1", f"{i}.md")
    repo.seed_file("alice", "vault1", "z-encrypted-looking.md")
    writer = FakeWriter()

    def read_file(_owner, _vault_id, path):
        if path == "z-encrypted-looking.md":
            return b"\xff\xfe\x00\x01not-utf8"
        return b"plain text"

    write_note(repo, "alice", "vault1", "new.md", "hello", read_file=read_file, write_file=writer, sample_size=3)

    assert writer.written[("alice", "vault1", "new.md")] == b"hello"


class FakeDeleter:
    """Stands in for the production delete_file callable, which does the real backup-then-remove
    disk work and reports back what actually happened."""

    def __init__(self, file_exists=True, deleted_at_ms=5000):
        self.file_exists = file_exists
        self.deleted_at_ms = deleted_at_ms
        self.calls = []

    def __call__(self, owner_username, vault_id, path):
        self.calls.append((owner_username, vault_id, path))
        backup_file_path = "/fake/last-version.bak" if self.file_exists else None
        return {"backup_file_path": backup_file_path, "deleted_at_ms": self.deleted_at_ms}


def test_delete_note_raises_for_missing_note():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        delete_note(repo, "alice", "vault1", "nope.md", delete_file=FakeDeleter())


def test_delete_note_raises_for_already_deleted_note():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "gone.md", is_deleted=True)
    with pytest.raises(ToolError):
        delete_note(repo, "alice", "vault1", "gone.md", delete_file=FakeDeleter())


def test_delete_note_marks_metadata_as_deleted():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", modified_at_ms=1, size_bytes=10, content_hash="h1")

    result = delete_note(repo, "alice", "vault1", "a.md", delete_file=FakeDeleter(deleted_at_ms=999))

    assert result == {"path": "a.md", "deleted": True}
    meta = repo.load_one("alice", "vault1", "a.md")
    assert meta["is_deleted"] is True
    assert meta["size_bytes"] == 0
    assert meta["content_hash"] == ""
    assert meta["modified_at_ms"] == 999


def test_delete_note_records_last_version_backup_and_tombstone_history_entries():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", modified_at_ms=1, size_bytes=10, content_hash="h1")

    delete_note(repo, "alice", "vault1", "a.md", delete_file=FakeDeleter(deleted_at_ms=999))

    assert len(repo.add_history_calls) == 2
    last_version_entry, tombstone_entry = repo.add_history_calls
    assert last_version_entry["backup_file_path"] == "/fake/last-version.bak"
    assert last_version_entry["content_hash"] == "h1"
    assert last_version_entry.get("deleted", False) is False

    assert tombstone_entry["deleted"] is True
    assert tombstone_entry["content_hash"] == ""
    assert tombstone_entry["size_bytes"] == 0
    assert tombstone_entry["backup_file_path"] == ""


def test_delete_note_skips_last_version_backup_when_file_already_absent_on_disk():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")

    delete_note(repo, "alice", "vault1", "a.md", delete_file=FakeDeleter(file_exists=False))

    # Only the tombstone entry -- no last-version backup entry when there was nothing on disk to
    # back up (metadata says the file exists, but it's already gone from the vault directory).
    assert len(repo.add_history_calls) == 1
    assert repo.add_history_calls[0]["deleted"] is True


class FakeMover:
    """Stands in for the production move_file callable."""

    def __init__(self, backup_file_path="/fake/renamed.bak"):
        self.calls = []
        self.backup_file_path = backup_file_path

    def __call__(self, owner_username, vault_id, old_path, new_path):
        self.calls.append((owner_username, vault_id, old_path, new_path))
        return {"backup_file_path": self.backup_file_path}


def test_rename_note_raises_when_source_missing():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        rename_note(repo, "alice", "vault1", "nope.md", "new.md", move_file=FakeMover())


def test_rename_note_raises_when_source_already_deleted():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", is_deleted=True)
    with pytest.raises(ToolError):
        rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=FakeMover())


def test_rename_note_refuses_to_overwrite_existing_active_note():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    repo.seed_file("alice", "vault1", "b.md")
    mover = FakeMover()

    with pytest.raises(ToolError, match="already exists"):
        rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=mover)
    assert mover.calls == []


def test_rename_note_allows_overwriting_a_deleted_path():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", content_hash="h1")
    repo.seed_file("alice", "vault1", "b.md", is_deleted=True)

    result = rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=FakeMover())

    assert result == {"old_path": "a.md", "new_path": "b.md"}


def test_rename_note_moves_the_physical_file():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    mover = FakeMover()

    rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=mover)

    assert mover.calls == [("alice", "vault1", "a.md", "b.md")]


def test_rename_note_migrates_metadata_to_new_path():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", content_hash="h1", size_bytes=10, modified_at_ms=5)

    rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=FakeMover())

    assert repo.load_one("alice", "vault1", "a.md") is None
    new_meta = repo.load_one("alice", "vault1", "b.md")
    assert new_meta["content_hash"] == "h1"
    assert new_meta["size_bytes"] == 10
    assert new_meta["modified_at_ms"] == 5
    assert new_meta["is_deleted"] is False


def test_rename_note_records_a_history_entry_with_related_path():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md", content_hash="h1", size_bytes=10, modified_at_ms=5)

    rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=FakeMover(backup_file_path="/fake/moved.bak"))

    assert len(repo.add_history_calls) == 1
    entry = repo.add_history_calls[0]
    assert entry["path"] == "b.md"
    assert entry["related_path"] == "a.md"
    assert entry["content_hash"] == "h1"
    assert entry["backup_file_path"] == "/fake/moved.bak"


def test_rename_note_moves_existing_history_entries():
    repo = FakeRepository()
    repo.seed_file("alice", "vault1", "a.md")
    repo.seed_history(1, "alice", "vault1", "a.md")

    rename_note(repo, "alice", "vault1", "a.md", "b.md", move_file=FakeMover())

    assert get_file_history(repo, "alice", "vault1", "a.md") == {"history": []}
    moved_history = get_file_history(repo, "alice", "vault1", "b.md")
    assert len(moved_history["history"]) == 2  # the pre-existing entry + the rename's own entry
