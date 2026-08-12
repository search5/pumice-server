"""TDD for RootResource's new /ws routing branch, added for the WebSocket sync endpoint
(#11_websocket_동기화_프로토콜_설계.md). Uses a plain fake resource double (just a `render`
method) rather than a real autobahn WebSocketResource -- RootResource only ever calls
`.render(request)` on whichever sub-resource matches, so that's the entire contract to verify.
"""

from pumice_server.grpc_web_resource import RootResource


class FakeResource:
    def __init__(self, marker):
        self.marker = marker
        self.rendered_with = None

    def render(self, request):
        self.rendered_with = request
        return self.marker


class FakeRequest:
    def __init__(self, path: bytes):
        self.path = path


def make_root_resource(ws_resource):
    return RootResource(
        sync_resource=FakeResource("sync"),
        fallback_resource=FakeResource("fallback"),
        ws_resource=ws_resource,
    )


def test_ws_path_is_routed_to_the_ws_resource():
    ws_resource = FakeResource("ws")
    root = make_root_resource(ws_resource)
    request = FakeRequest(b"/ws")

    result = root.render(request)

    assert result == "ws"
    assert ws_resource.rendered_with is request


def test_other_paths_are_unaffected_by_ws_resource_being_present():
    ws_resource = FakeResource("ws")
    root = make_root_resource(ws_resource)

    assert root.render(FakeRequest(b"/some/other/path")) == "fallback"


def test_ws_path_falls_through_to_fallback_when_ws_resource_is_absent():
    root = RootResource(sync_resource=FakeResource("sync"), fallback_resource=FakeResource("fallback"))

    assert root.render(FakeRequest(b"/ws")) == "fallback"
