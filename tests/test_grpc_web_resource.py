"""response_content_type() must always return one of the two valid gRPC-Web Content-Type
values -- never an echo of the client's raw Accept header. Regression test for the bug where
real browser/Electron clients (grpc-web JS doesn't set an Accept header in binary mode, so the
HTTP stack defaults it to "*/*") got "*/*" echoed straight back as Content-Type, which grpc-web's
own client then rejects as an unrecognized response Content-Type ("Unknown Content-type received").
"""

from pumice_server.grpc_web_resource import response_content_type


def test_binary_mode_returns_grpc_web_proto():
    assert response_content_type(is_text=False) == b"application/grpc-web+proto"


def test_text_mode_returns_grpc_web_text():
    assert response_content_type(is_text=True) == b"application/grpc-web-text"
