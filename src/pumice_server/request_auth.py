"""Device-token auth resolution from a raw twisted.web Request -- split out of
streaming_upload_resource.py in PR5 (see pumice's 13_PR5_계획.md) when that module's actual
streaming-upload machinery (StreamingUploadRequest/_UploadAccumulator/StreamingUploadResource,
all gRPC-Web-specific) was deleted alongside gRPC-Web itself. This one function is still
genuinely needed by mcp_resource.py -- the MCP endpoint isn't a sync RPC and has nothing to do
with gRPC-Web, it just happened to reuse this same header-parsing logic.

Mirrors the token-resolution half of SyncServiceServicer._verify_vault_access (service.py),
but works directly off a Request's headers rather than a ServicerContext's
invocation_metadata(), since callers here run before/outside any RPC dispatch.
"""

from twisted.web.server import Request


class StreamingAuthError(Exception):
    """Raised when the Authorization header is missing or doesn't resolve to a known device
    token."""


def resolve_owner_username(request: Request, repository) -> str:
    raw = request.requestHeaders.getRawHeaders(b"authorization")
    if not raw:
        raise StreamingAuthError("Missing authorization header")
    auth_header = raw[0].decode("utf-8", errors="replace")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header.strip()
    if not token:
        raise StreamingAuthError("Missing authorization header")

    device = repository.get_device_token(token)
    if not device:
        raise StreamingAuthError("Invalid or missing auth token")
    return device["username"]
