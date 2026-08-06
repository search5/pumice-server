"""Tool name -> handler dispatch and argument validation for the MCP tools (5절 of
MCP_STREAMABLE_HTTP_구현_계획.md, plus get_recent_notes/search_notes added afterward). Deliberately
kept independent of the mcp SDK's
types.Tool/CallToolResult -- McpResource's on_list_tools/on_call_tool wrap TOOL_SCHEMAS/
TOOL_DESCRIPTIONS and dispatch_tool_call()'s result into those SDK shapes, but this module itself
has no SDK import, so it's directly unit-testable (see test_mcp_tool_registry.py).
"""

from typing import Any, Callable, Optional

from . import mcp_tools
from .mcp_tools import ToolError

TOOL_SCHEMAS: dict[str, dict] = {
    "list_vaults": {"type": "object", "properties": {}},
    "list_files": {
        "type": "object",
        "properties": {"vault_id": {"type": "string"}},
        "required": ["vault_id"],
    },
    "get_file_history": {
        "type": "object",
        "properties": {"vault_id": {"type": "string"}, "path": {"type": "string"}},
        "required": ["vault_id", "path"],
    },
    "read_file_version": {
        "type": "object",
        "properties": {
            "history_id": {"type": "integer"},
            "e2ee_password": {
                "type": "string",
                "description": "Required only if this vault has end-to-end encryption enabled.",
            },
        },
        "required": ["history_id"],
    },
    "list_published_files": {
        "type": "object",
        "properties": {"vault_id": {"type": "string"}},
        "required": ["vault_id"],
    },
    "get_recent_notes": {
        "type": "object",
        "properties": {
            "vault_id": {"type": "string"},
            "limit": {"type": "integer", "description": "Max number of files to return (default 20)."},
        },
        "required": ["vault_id"],
    },
    "search_notes": {
        "type": "object",
        "properties": {
            "vault_id": {"type": "string"},
            "query": {"type": "string"},
            "e2ee_password": {
                "type": "string",
                "description": "Required only if this vault has end-to-end encryption enabled.",
            },
        },
        "required": ["vault_id", "query"],
    },
    "read_note": {
        "type": "object",
        "properties": {
            "vault_id": {"type": "string"},
            "path": {"type": "string"},
            "e2ee_password": {
                "type": "string",
                "description": "Required only if this vault has end-to-end encryption enabled.",
            },
        },
        "required": ["vault_id", "path"],
    },
    "search_by_frontmatter": {
        "type": "object",
        "properties": {
            "vault_id": {"type": "string"},
            "key": {"type": "string", "description": "Frontmatter property name, e.g. 'status' or 'tags'."},
            "value": {"type": "string", "description": "Substring to match within that property's value."},
            "e2ee_password": {
                "type": "string",
                "description": "Required only if this vault has end-to-end encryption enabled.",
            },
        },
        "required": ["vault_id", "key", "value"],
    },
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_vaults": "List every vault this account has synced to this server.",
    "list_files": "List the current (non-deleted) files in a vault, with path/size/hash/modified time.",
    "get_file_history": "List the version history for one file in a vault.",
    "read_file_version": (
        "Read the content of one specific historical version of a file, by its history_id "
        "(from get_file_history). If the vault has end-to-end encryption enabled, pass "
        "e2ee_password to decrypt the content -- otherwise the raw stored bytes are returned "
        "as-is, which will be ciphertext if the vault is encrypted."
    ),
    "list_published_files": "List files currently published (shared publicly) from a vault.",
    "get_recent_notes": "List the most recently modified files in a vault, newest first.",
    "search_notes": (
        "Full-text search across a vault's current files. If the vault has end-to-end encryption "
        "enabled, pass e2ee_password to search decrypted content -- otherwise encrypted files are "
        "silently skipped (their content can't be matched against as ciphertext)."
    ),
    "read_note": (
        "Read a note's current content by path -- the one-call equivalent of get_file_history + "
        "read_file_version(latest history_id). If the vault has end-to-end encryption enabled, "
        "pass e2ee_password to decrypt the content."
    ),
    "search_by_frontmatter": (
        "Find notes whose YAML frontmatter has a given key containing a given value (substring, "
        "case insensitive -- e.g. key='tags', value='urgent' matches `tags: [project, urgent]`). "
        "If the vault has end-to-end encryption enabled, pass e2ee_password to search decrypted "
        "content -- otherwise encrypted files are silently skipped."
    ),
}


def _require_str(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} is required and must be a non-empty string")
    return value


def _require_positive_int(arguments: dict, key: str, default: int) -> int:
    if key not in arguments:
        return default
    value = arguments[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ToolError(f"{key} must be a positive integer")
    return value


def dispatch_tool_call(
    repository,
    owner_username: str,
    tool_name: str,
    arguments: Optional[dict[str, Any]],
    read_bytes: Optional[Callable[[str], bytes]] = None,
    read_vault_file: Optional[Callable[[str, str, str], bytes]] = None,
) -> dict:
    arguments = arguments or {}

    if tool_name == "list_vaults":
        return mcp_tools.list_vaults(repository, owner_username)

    if tool_name == "list_files":
        vault_id = _require_str(arguments, "vault_id")
        return mcp_tools.list_files(repository, owner_username, vault_id)

    if tool_name == "get_file_history":
        vault_id = _require_str(arguments, "vault_id")
        path = _require_str(arguments, "path")
        return mcp_tools.get_file_history(repository, owner_username, vault_id, path)

    if tool_name == "read_file_version":
        history_id = arguments.get("history_id")
        if not isinstance(history_id, int) or isinstance(history_id, bool):
            raise ToolError("history_id is required and must be an integer")
        e2ee_password = arguments.get("e2ee_password") or None
        return mcp_tools.read_file_version(
            repository, owner_username, history_id, e2ee_password=e2ee_password, read_bytes=read_bytes
        )

    if tool_name == "list_published_files":
        vault_id = _require_str(arguments, "vault_id")
        return mcp_tools.list_published_files(repository, owner_username, vault_id)

    if tool_name == "get_recent_notes":
        vault_id = _require_str(arguments, "vault_id")
        limit = _require_positive_int(arguments, "limit", default=20)
        return mcp_tools.get_recent_notes(repository, owner_username, vault_id, limit=limit)

    if tool_name == "search_notes":
        vault_id = _require_str(arguments, "vault_id")
        query = _require_str(arguments, "query")
        if read_vault_file is None:
            raise ToolError("search_notes is not available: no vault file reader was configured")
        e2ee_password = arguments.get("e2ee_password") or None
        return mcp_tools.search_notes(
            repository, owner_username, vault_id, query, read_file=read_vault_file, e2ee_password=e2ee_password
        )

    if tool_name == "read_note":
        vault_id = _require_str(arguments, "vault_id")
        path = _require_str(arguments, "path")
        if read_vault_file is None:
            raise ToolError("read_note is not available: no vault file reader was configured")
        e2ee_password = arguments.get("e2ee_password") or None
        return mcp_tools.read_note(
            repository, owner_username, vault_id, path, read_file=read_vault_file, e2ee_password=e2ee_password
        )

    if tool_name == "search_by_frontmatter":
        vault_id = _require_str(arguments, "vault_id")
        key = _require_str(arguments, "key")
        value = _require_str(arguments, "value")
        if read_vault_file is None:
            raise ToolError("search_by_frontmatter is not available: no vault file reader was configured")
        e2ee_password = arguments.get("e2ee_password") or None
        return mcp_tools.search_by_frontmatter(
            repository, owner_username, vault_id, key, value, read_file=read_vault_file, e2ee_password=e2ee_password
        )

    raise ToolError(f"Unknown tool: {tool_name}")
