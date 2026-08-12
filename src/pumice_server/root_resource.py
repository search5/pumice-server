"""Routes /ws and /mcp to their native resources and everything else (the Pyramid web UI, the
admin API, publish, etc.) to a WSGIResource fallback, on the same port.

Split out of grpc_web_resource.py in PR5 (see pumice's 13_PR5_계획.md) when gRPC-Web and SSE
/watch were fully retired in favor of the single WS sync endpoint -- this class itself never
had any gRPC-Web-specific logic, it just used to live alongside the code that did (and routed
to it: the old sync/streaming-upload/watch branches are gone along with those resources)."""

from twisted.web.resource import Resource


class RootResource(Resource):
    isLeaf = True

    def __init__(self, fallback_resource, mcp_resource=None, ws_resource=None):
        super().__init__()
        self._fallback_resource = fallback_resource
        self._mcp_resource = mcp_resource
        self._ws_resource = ws_resource

    def render(self, request):
        # /ws is the WebSocket sync endpoint -- see ws_sync_resource.py's module docstring
        # and #11_websocket_동기화_프로토콜_설계.md.
        if self._ws_resource is not None and request.path == b"/ws":
            return self._ws_resource.render(request)
        # /mcp is the MCP Streamable HTTP endpoint -- plain JSON-RPC-over-HTTP, see
        # mcp_resource.py's module docstring.
        if self._mcp_resource is not None and request.path == b"/mcp":
            return self._mcp_resource.render(request)
        return self._fallback_resource.render(request)
