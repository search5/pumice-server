"""TDD for tool-name -> handler dispatch and argument validation (part of 5절/11-2단계) --
independent of the mcp SDK's types.Tool/CallToolResult wrapping, which is thin glue tested manually
per 10절 (실제 MCP 클라이언트 연동 확인). This only tests "given a tool name and a raw arguments
dict, does the right pumice-server call happen with the right, validated arguments."
"""

import pytest

from pumice_server.mcp_tools import ToolError
from pumice_server.mcp_tool_registry import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, dispatch_tool_call


class FakeRepository:
    def __init__(self):
        self.calls = []

    def get_vaults_by_owner(self, owner_username):
        self.calls.append(("get_vaults_by_owner", owner_username))
        return ["vault1"]

    def load_all(self, owner_username, vault_id):
        self.calls.append(("load_all", owner_username, vault_id))
        return {
            "a.md": {
                "path": "a.md",
                "modified_at_ms": 1,
                "size_bytes": 1,
                "content_hash": "h",
                "is_deleted": False,
            }
        }

    def get_history(self, owner_username, vault_id, path):
        self.calls.append(("get_history", owner_username, vault_id, path))
        return []

    def get_history_by_id(self, owner_username, history_id):
        self.calls.append(("get_history_by_id", owner_username, history_id))
        return {
            "path": "a.md",
            "vault_id": "vault1",
            "backup_file_path": "/fake/1.bak",
            "deleted": False,
        }

    def get_published_files(self, owner_username, vault_id):
        self.calls.append(("get_published_files", owner_username, vault_id))
        return []

    def load_one(self, owner_username, vault_id, path):
        self.calls.append(("load_one", owner_username, vault_id, path))
        if path != "a.md":
            return None
        return {"path": path, "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h", "is_deleted": False}

    def save_one(self, owner_username, vault_id, path, meta):
        self.calls.append(("save_one", owner_username, vault_id, path, meta))

    def add_history(self, **kwargs):
        self.calls.append(("add_history", kwargs))

    def migrate_history_on_rename(self, owner_username, vault_id, old_path, new_path):
        self.calls.append(("migrate_history_on_rename", owner_username, vault_id, old_path, new_path))


def test_all_twelve_tools_have_schemas_and_descriptions():
    expected = {
        "list_vaults",
        "list_files",
        "get_file_history",
        "read_file_version",
        "list_published_files",
        "get_recent_notes",
        "search_notes",
        "read_note",
        "search_by_frontmatter",
        "write_note",
        "delete_note",
        "rename_note",
    }
    assert set(TOOL_SCHEMAS.keys()) == expected
    assert set(TOOL_DESCRIPTIONS.keys()) == expected


def test_list_vaults_dispatch():
    repo = FakeRepository()
    result = dispatch_tool_call(repo, "alice", "list_vaults", {})
    assert result == {"vaults": ["vault1"]}
    assert repo.calls == [("get_vaults_by_owner", "alice")]


def test_list_files_dispatch_requires_vault_id():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "list_files", {})


def test_list_files_dispatch_passes_vault_id():
    repo = FakeRepository()
    dispatch_tool_call(repo, "alice", "list_files", {"vault_id": "vault1"})
    assert repo.calls == [("load_all", "alice", "vault1")]


def test_get_file_history_dispatch_requires_vault_id_and_path():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_file_history", {"vault_id": "vault1"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_file_history", {"path": "a.md"})


def test_get_file_history_dispatch_passes_both_args():
    repo = FakeRepository()
    dispatch_tool_call(repo, "alice", "get_file_history", {"vault_id": "vault1", "path": "a.md"})
    assert repo.calls == [("get_history", "alice", "vault1", "a.md")]


def test_read_file_version_dispatch_requires_integer_history_id():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "read_file_version", {})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "read_file_version", {"history_id": "not-an-int"})


def test_read_file_version_dispatch_passes_optional_password():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo, "alice", "read_file_version", {"history_id": 1}, read_bytes=lambda _p: b"hello"
    )
    assert result == {"path": "a.md", "content": "hello", "encoding": "utf-8"}


def test_list_published_files_dispatch_requires_vault_id():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "list_published_files", {})


def test_list_published_files_dispatch_passes_vault_id():
    repo = FakeRepository()
    dispatch_tool_call(repo, "alice", "list_published_files", {"vault_id": "vault1"})
    assert repo.calls == [("get_published_files", "alice", "vault1")]


def test_unknown_tool_raises():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="Unknown tool"):
        dispatch_tool_call(repo, "alice", "delete_everything", {})


def test_arguments_none_is_treated_as_empty_dict():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "list_files", None)


def test_get_recent_notes_dispatch_requires_vault_id():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_recent_notes", {})


def test_get_recent_notes_dispatch_passes_vault_id_and_defaults_limit():
    repo = FakeRepository()
    result = dispatch_tool_call(repo, "alice", "get_recent_notes", {"vault_id": "vault1"})
    assert repo.calls == [("load_all", "alice", "vault1")]
    assert result == {"files": [{"path": "a.md", "modified_at_ms": 1, "size_bytes": 1, "content_hash": "h"}]}


def test_get_recent_notes_dispatch_passes_explicit_limit():
    repo = FakeRepository()
    dispatch_tool_call(repo, "alice", "get_recent_notes", {"vault_id": "vault1", "limit": 5})
    # FakeRepository only ever has one file, so this just confirms the call doesn't raise --
    # get_recent_notes's own limit-slicing behavior is covered in test_mcp_tools.py.
    assert repo.calls == [("load_all", "alice", "vault1")]


def test_get_recent_notes_dispatch_rejects_non_integer_limit():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_recent_notes", {"vault_id": "vault1", "limit": "lots"})


def test_get_recent_notes_dispatch_rejects_zero_or_negative_limit():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_recent_notes", {"vault_id": "vault1", "limit": 0})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "get_recent_notes", {"vault_id": "vault1", "limit": -1})


def test_search_notes_dispatch_requires_vault_id_and_query():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "search_notes", {"vault_id": "vault1"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "search_notes", {"query": "x"})


def test_search_notes_dispatch_passes_args_and_uses_injected_read_vault_file():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "search_notes",
        {"vault_id": "vault1", "query": "hello"},
        read_vault_file=lambda _owner, _vault_id, _path: b"hello world",
    )
    assert repo.calls == [("load_all", "alice", "vault1")]
    assert result == {"matches": [{"path": "a.md", "snippet": "hello world"}], "truncated": False}


def test_search_notes_dispatch_passes_optional_password_through():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "search_notes",
        {"vault_id": "vault1", "query": "hello", "e2ee_password": ""},
        read_vault_file=lambda _owner, _vault_id, _path: b"hello world",
    )
    # An empty-string password is treated the same as "no password", same convention as
    # read_file_version's dispatch handling.
    assert result["matches"] == [{"path": "a.md", "snippet": "hello world"}]


def test_read_note_dispatch_requires_vault_id_and_path():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "read_note", {"vault_id": "vault1"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "read_note", {"path": "a.md"})


def test_read_note_dispatch_requires_a_configured_reader():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="reader"):
        dispatch_tool_call(repo, "alice", "read_note", {"vault_id": "vault1", "path": "a.md"})


def test_read_note_dispatch_passes_args_and_uses_injected_reader():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "read_note",
        {"vault_id": "vault1", "path": "a.md"},
        read_vault_file=lambda _owner, _vault_id, _path: b"current content",
    )
    assert repo.calls == [("load_one", "alice", "vault1", "a.md")]
    assert result == {"path": "a.md", "content": "current content", "encoding": "utf-8"}


def test_search_by_frontmatter_dispatch_requires_vault_id_key_and_value():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "search_by_frontmatter", {"vault_id": "vault1", "key": "status"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "search_by_frontmatter", {"key": "status", "value": "done"})


def test_search_by_frontmatter_dispatch_requires_a_configured_reader():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="reader"):
        dispatch_tool_call(
            repo, "alice", "search_by_frontmatter", {"vault_id": "vault1", "key": "status", "value": "done"}
        )


def test_search_by_frontmatter_dispatch_passes_args_and_uses_injected_reader():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "search_by_frontmatter",
        {"vault_id": "vault1", "key": "status", "value": "done"},
        read_vault_file=lambda _owner, _vault_id, _path: b"---\nstatus: done\n---\nbody",
    )
    assert repo.calls == [("load_all", "alice", "vault1")]
    assert result == {"paths": ["a.md"], "truncated": False}


def _fake_write_vault_file(_owner, _vault_id, _path, raw):
    return {"modified_at_ms": 1, "size_bytes": len(raw), "backup_file_path": "/fake/x.bak"}


def test_write_note_dispatch_requires_vault_id_path_and_content():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "write_note", {"vault_id": "vault1", "path": "a.md"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "write_note", {"path": "a.md", "content": "x"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "write_note", {"vault_id": "vault1", "content": "x"})


def test_write_note_dispatch_requires_configured_readers_and_writer():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="reader|writer"):
        dispatch_tool_call(repo, "alice", "write_note", {"vault_id": "vault1", "path": "a.md", "content": "x"})
    with pytest.raises(ToolError, match="reader|writer"):
        dispatch_tool_call(
            repo,
            "alice",
            "write_note",
            {"vault_id": "vault1", "path": "a.md", "content": "x"},
            read_vault_file=lambda *_: b"plain",
        )


def test_write_note_dispatch_passes_args_and_uses_injected_reader_and_writer():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "write_note",
        {"vault_id": "vault1", "path": "a.md", "content": "hello world"},
        read_vault_file=lambda *_: b"plain",
        write_vault_file=_fake_write_vault_file,
    )
    assert result == {"path": "a.md", "size_bytes": 11, "encrypted": False}

    save_one_calls = [c for c in repo.calls if c[0] == "save_one"]
    assert len(save_one_calls) == 1
    _, owner, vault_id, path, meta = save_one_calls[0]
    assert (owner, vault_id, path) == ("alice", "vault1", "a.md")
    assert meta["modified_at_ms"] == 1
    assert meta["size_bytes"] == 11
    assert meta["is_deleted"] is False
    assert meta["content_hash"]

    add_history_calls = [c for c in repo.calls if c[0] == "add_history"]
    assert len(add_history_calls) == 1


def test_write_note_dispatch_passes_optional_password_through():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "write_note",
        {"vault_id": "vault1", "path": "a.md", "content": "hello world", "e2ee_password": ""},
        read_vault_file=lambda *_: b"plain",
        write_vault_file=_fake_write_vault_file,
    )
    # Empty-string password treated as "no password", same convention as the other tools.
    assert result["encrypted"] is False


def _fake_delete_vault_file(_owner, _vault_id, _path):
    return {"backup_file_path": "/fake/last.bak", "deleted_at_ms": 42}


def test_delete_note_dispatch_requires_vault_id_and_path():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "delete_note", {"vault_id": "vault1"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "delete_note", {"path": "a.md"})


def test_delete_note_dispatch_requires_a_configured_deleter():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="deleter"):
        dispatch_tool_call(repo, "alice", "delete_note", {"vault_id": "vault1", "path": "a.md"})


def test_delete_note_dispatch_passes_args_and_uses_injected_deleter():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "delete_note",
        {"vault_id": "vault1", "path": "a.md"},
        delete_vault_file=_fake_delete_vault_file,
    )
    assert result == {"path": "a.md", "deleted": True}
    assert ("load_one", "alice", "vault1", "a.md") in repo.calls


def _fake_move_vault_file(_owner, _vault_id, _old_path, _new_path):
    return {"backup_file_path": "/fake/moved.bak"}


def test_rename_note_dispatch_requires_vault_id_old_path_and_new_path():
    repo = FakeRepository()
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "rename_note", {"vault_id": "vault1", "old_path": "a.md"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "rename_note", {"vault_id": "vault1", "new_path": "b.md"})
    with pytest.raises(ToolError):
        dispatch_tool_call(repo, "alice", "rename_note", {"old_path": "a.md", "new_path": "b.md"})


def test_rename_note_dispatch_requires_a_configured_mover():
    repo = FakeRepository()
    with pytest.raises(ToolError, match="mover"):
        dispatch_tool_call(
            repo, "alice", "rename_note", {"vault_id": "vault1", "old_path": "a.md", "new_path": "b.md"}
        )


def test_rename_note_dispatch_passes_args_and_uses_injected_mover():
    repo = FakeRepository()
    result = dispatch_tool_call(
        repo,
        "alice",
        "rename_note",
        {"vault_id": "vault1", "old_path": "a.md", "new_path": "b.md"},
        move_vault_file=_fake_move_vault_file,
    )
    assert result == {"old_path": "a.md", "new_path": "b.md"}
    assert ("migrate_history_on_rename", "alice", "vault1", "a.md", "b.md") in repo.calls

