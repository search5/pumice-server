"""Twisted Resource exposing pumice-server's MCP (Model Context Protocol) tools over Streamable
HTTP (protocol version 2025-06-18). See MCP_STREAMABLE_HTTP_구현_계획.md for the full design --
this follows section 6-9 exactly: one mcp.server.lowlevel.Server instance + one pair of anyio
memory-object streams + one asyncio task per session (kept alive across the session's HTTP
requests, not recreated per-request), driven the same way SyncServiceResource drives its own
asyncio coroutines (grpc_web_resource.py's render()/asyncio.ensure_future() pattern).

Deliberately thin: JSON-RPC parsing (mcp_types.jsonrpc_message_adapter), tool listing/dispatch
(mcp_tool_registry.py), and the actual tool logic (mcp_tools.py) all live in pure, independently
unit-tested modules. This file is the transport glue tying them to Twisted + the SDK's Server
class, verified manually (10절) rather than by unit test, same split as elsewhere in this codebase.
"""

import asyncio
import importlib.metadata
import json
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional

import anyio
import mcp.server.lowlevel as lowlevel
import mcp.types as types
import pydantic_core
from mcp.server.models import InitializationOptions
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCRequest, jsonrpc_message_adapter
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET, Request

from .mcp_tool_registry import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, dispatch_tool_call
from .mcp_tools import ToolError, resolve_vault_file_path
from .streaming_upload_resource import StreamingAuthError, resolve_owner_username

logger = logging.getLogger(__name__)

_MCP_SESSION_ID_HEADER = b"mcp-session-id"
_SERVER_NAME = "pumice-server"
# Kept as its own lookup (not hardcoded) so this doesn't silently drift from pyproject.toml's
# actual released version -- falls back to a clearly-a-placeholder string rather than crashing if
# the package metadata isn't available (e.g. running from a source checkout without an install).
try:
    _SERVER_VERSION = importlib.metadata.version("pumice-server")
except importlib.metadata.PackageNotFoundError:
    _SERVER_VERSION = "0.0.0-dev"

_CORS_ALLOW_HEADERS = b"authorization, content-type, mcp-session-id, mcp-protocol-version"

# A tool call is just a few DB reads -- this only guards against a wedged/crashed session task
# leaving a request hanging forever, not a legitimately slow operation.
_RESPONSE_TIMEOUT_SECONDS = 30.0


def _build_tool_list() -> list:
    return [
        types.Tool(name=name, description=TOOL_DESCRIPTIONS[name], inputSchema=TOOL_SCHEMAS[name])
        for name in TOOL_SCHEMAS
    ]


class _McpSession:
    __slots__ = ("owner_username", "send_to_server", "receive_from_server", "task")

    def __init__(self, owner_username, send_to_server, receive_from_server, task):
        self.owner_username = owner_username
        self.send_to_server = send_to_server
        self.receive_from_server = receive_from_server
        self.task = task


class McpResource(Resource):
    isLeaf = True

    def __init__(self, repository, data_dir, reactor=None):
        super().__init__()
        self._repository = repository
        self._data_dir = data_dir
        self._sessions: Dict[str, _McpSession] = {}

    def render_OPTIONS(self, request: Request) -> bytes:
        origin = request.getHeader(b"origin") or b"*"
        request.setResponseCode(204)
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Methods", b"POST, GET, OPTIONS")
        request.setHeader(b"Access-Control-Allow-Headers", _CORS_ALLOW_HEADERS)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")
        request.setHeader(b"Content-Length", b"0")
        return b""

    def render_GET(self, request: Request) -> bytes:
        # Server-initiated notifications (the GET/SSE half of Streamable HTTP) aren't implemented
        # yet -- this server has nothing to push proactively right now (12절 알려진 제한사항).
        self._cors_headers(request)
        request.setResponseCode(501)
        request.setHeader(b"Content-Type", b"text/plain")
        return b"Server-initiated notifications are not implemented yet."

    def render_POST(self, request: Request) -> bytes:
        task = asyncio.ensure_future(self._handle_post(request))
        task.add_done_callback(lambda t: self._on_task_done(t, request))
        return NOT_DONE_YET

    def _on_task_done(self, task: "asyncio.Task", request: Request) -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc is None:
            return
        logger.error("Unhandled error serving MCP request", exc_info=exc)
        try:
            if not request.finished:
                request.setResponseCode(500)
                request.finish()
        except Exception:
            pass

    def _cors_headers(self, request: Request) -> None:
        origin = request.getHeader(b"origin") or b"*"
        request.setHeader(b"Access-Control-Allow-Origin", origin)
        request.setHeader(b"Access-Control-Allow-Credentials", b"true")

    def _write_error(self, request: Request, status: int, message: str, request_id=None) -> None:
        self._cors_headers(request)
        request.setResponseCode(status)
        request.setHeader(b"Content-Type", b"application/json")
        if status == 401:
            # RFC 9728 discovery hint: an MCP client with no (or an expired) credential learns
            # where to find this server's OAuth protected-resource metadata from here, which is
            # what lets "just paste the server URL" bootstrap into a browser-based auth flow
            # instead of the client having no idea auth is even possible.
            scheme = "https" if request.isSecure() else "http"
            host = (request.getHeader(b"host") or b"").decode("utf-8", errors="replace")
            resource_metadata_url = f"{scheme}://{host}/.well-known/oauth-protected-resource"
            request.setHeader(b"WWW-Authenticate", f'Bearer resource_metadata="{resource_metadata_url}"'.encode("utf-8"))
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}}).encode(
            "utf-8"
        )
        request.write(body)
        if not request.finished:
            request.finish()

    async def _handle_post(self, request: Request) -> None:
        try:
            # Deliberately asyncio.to_thread(), not streaming_upload_resource.py's own
            # resolve_owner_via_thread() -- that helper returns a Twisted Deferred, and this
            # whole request is already running inside a plain asyncio.Task (see render_POST),
            # not Twisted's own Deferred-driven coroutine machinery, so a raw `await`
            # on a Deferred here fails with "Task got bad yield". asyncio.to_thread's Future
            # is natively awaitable from a plain Task, same as everything else in this method.
            owner_username = await asyncio.to_thread(resolve_owner_username, request, self._repository)
        except StreamingAuthError as e:
            self._write_error(request, 401, str(e))
            return

        body = request.content.read()
        try:
            raw = pydantic_core.from_json(body)
        except ValueError as e:
            self._write_error(request, 400, f"Parse error: {e}")
            return

        try:
            message = jsonrpc_message_adapter.validate_python(raw)
        except Exception as e:
            self._write_error(request, 400, f"Invalid Request: {e}")
            return

        is_request = isinstance(message, JSONRPCRequest)
        is_initialize = is_request and message.method == "initialize"
        request_id = getattr(message, "id", None)

        session_id: Optional[str]
        session_id_header = request.getHeader(_MCP_SESSION_ID_HEADER)
        session_id = session_id_header.decode("utf-8", errors="replace") if session_id_header else None

        if is_initialize:
            session = self._create_session(owner_username)
            session_id = secrets.token_hex(16)
            self._sessions[session_id] = session
        else:
            session = self._sessions.get(session_id) if session_id else None
            if session is None:
                self._write_error(request, 404, "Not Found: invalid or expired session ID", request_id=request_id)
                return
            if session.owner_username != owner_username:
                # A session's owner is fixed at creation (the initialize call that made it) -- a
                # later request reusing that session id must never be honored under a *different*
                # account's token, even if the session id itself is otherwise still valid.
                self._write_error(request, 401, "Session does not belong to this account", request_id=request_id)
                return

        await session.send_to_server.send(SessionMessage(message))

        if not is_request:
            # A notification or a response *from* the client -- MCP never replies to either.
            self._cors_headers(request)
            request.setResponseCode(202)
            if not request.finished:
                request.finish()
            return

        try:
            response_session_message = await asyncio.wait_for(
                session.receive_from_server.receive(), timeout=_RESPONSE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._write_error(request, 500, "Timed out waiting for a response from the MCP session", request_id=request_id)
            return

        response_message = response_session_message.message
        body_out = response_message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")

        self._cors_headers(request)
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"application/json")
        if is_initialize:
            request.setHeader(_MCP_SESSION_ID_HEADER, session_id.encode("utf-8"))
        request.write(body_out)
        if not request.finished:
            request.finish()

    def _read_vault_file(self, owner_username: str, vault_id: str, path: str) -> bytes:
        full_path = resolve_vault_file_path(self._data_dir, owner_username, vault_id, path)
        with open(full_path, "rb") as f:
            return f.read()

    def _write_vault_file(self, owner_username: str, vault_id: str, path: str, raw: bytes) -> dict:
        """Writes raw bytes as a note's new current content, then records a history backup --
        mirrors service.py's own write path exactly (temp-file-then-rename, then hard-link the
        result into history/): _backup_file's hard-link trick is only safe because a live file is
        never edited in place, always replaced via a fresh temp file + os.rename(), so a past
        backup's inode is never mutated retroactively. Deliberately skips service.py's
        same-hash-skip-backup dedup optimization -- write_note is an occasional agent-driven write,
        not continuous sync traffic, so the extra backup-per-write cost doesn't matter here, and
        skipping the dedup check keeps this simpler."""
        final_path = resolve_vault_file_path(self._data_dir, owner_username, vault_id, path)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        temp_dir = os.path.join(self._data_dir, "tmp", owner_username, vault_id)
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"{secrets.token_hex(8)}.tmp")
        with open(temp_path, "wb") as f:
            f.write(raw)

        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(temp_path, final_path)

        stat = os.stat(final_path)
        modified_at_ms = int(stat.st_mtime * 1000)
        size_bytes = stat.st_size
        backup_file_path = self._backup_vault_file(owner_username, vault_id, final_path)

        return {"modified_at_ms": modified_at_ms, "size_bytes": size_bytes, "backup_file_path": backup_file_path}

    def _backup_vault_file(self, owner_username: str, vault_id: str, source_path: str) -> str:
        """Hard-links (or, failing that, copies) source_path into history/ -- shared by
        _write_vault_file/_delete_vault_file/_move_vault_file, mirroring service.py's
        _backup_file exactly."""
        backup_dir = os.path.join(self._data_dir, "history", owner_username, vault_id)
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = int(time.time() * 1000)
        backup_file_path = os.path.join(backup_dir, f"{timestamp}_{secrets.token_hex(4)}.bak")
        try:
            os.link(source_path, backup_file_path)
        except OSError:
            shutil.copy2(source_path, backup_file_path)
        return backup_file_path

    def _delete_vault_file(self, owner_username: str, vault_id: str, path: str) -> dict:
        """Backs up the last physical version (if it still exists on disk) then removes it --
        mirrors service.py's _delete_on_server exactly."""
        full_path = resolve_vault_file_path(self._data_dir, owner_username, vault_id, path)
        backup_file_path = None
        if os.path.exists(full_path):
            backup_file_path = self._backup_vault_file(owner_username, vault_id, full_path)
            os.remove(full_path)
        return {"backup_file_path": backup_file_path, "deleted_at_ms": int(time.time() * 1000)}

    def _move_vault_file(self, owner_username: str, vault_id: str, old_path: str, new_path: str) -> dict:
        """Moves a note's live file to a new path, then backs up the result -- mirrors
        service.py's own rename-detected write path (_finalize_uploaded_file always backs up the
        file at its new location, even though content is unchanged)."""
        old_full_path = resolve_vault_file_path(self._data_dir, owner_username, vault_id, old_path)
        new_full_path = resolve_vault_file_path(self._data_dir, owner_username, vault_id, new_path)
        os.makedirs(os.path.dirname(new_full_path), exist_ok=True)
        os.rename(old_full_path, new_full_path)
        backup_file_path = self._backup_vault_file(owner_username, vault_id, new_full_path)
        return {"backup_file_path": backup_file_path}

    def _create_session(self, owner_username: str) -> _McpSession:
        client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
        server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)
        repository = self._repository

        @asynccontextmanager
        async def lifespan(_server):
            yield {"repository": repository, "owner_username": owner_username}

        async def on_list_tools(ctx, params):
            return types.ListToolsResult(tools=_build_tool_list())

        async def on_call_tool(ctx, params):
            lifespan_ctx = ctx.lifespan_context
            try:
                # asyncio.to_thread(), not threads.deferToThread() -- same reasoning as
                # _handle_post's owner resolution above: this coroutine runs inside a plain
                # asyncio.Task (server.run(), started via asyncio.ensure_future in
                # _create_session), which can't directly await a Twisted Deferred.
                result = await asyncio.to_thread(
                    dispatch_tool_call,
                    lifespan_ctx["repository"],
                    lifespan_ctx["owner_username"],
                    params.name,
                    params.arguments,
                    read_vault_file=self._read_vault_file,
                    write_vault_file=self._write_vault_file,
                    delete_vault_file=self._delete_vault_file,
                    move_vault_file=self._move_vault_file,
                )
                text = json.dumps(result, ensure_ascii=False)
                return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
            except ToolError as e:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(e))], isError=True)

        server = lowlevel.Server(
            _SERVER_NAME,
            version=_SERVER_VERSION,
            lifespan=lifespan,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
        init_options = InitializationOptions(
            server_name=_SERVER_NAME,
            server_version=_SERVER_VERSION,
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
        )

        task = asyncio.ensure_future(server.run(client_to_server_receive, server_to_client_send, init_options))
        task.add_done_callback(self._on_session_task_done)

        return _McpSession(owner_username, client_to_server_send, server_to_client_receive, task)

    def _on_session_task_done(self, task: "asyncio.Task") -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error("MCP session task ended with an error", exc_info=exc)
