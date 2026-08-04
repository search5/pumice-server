"""watch_resource.py's render_OPTIONS was missing entirely, so any browser cross-origin GET to
/watch with a custom Authorization header (which always requires a CORS preflight) got a bare
405 with no CORS headers back from Twisted's default Resource.render() -- the browser then
blocks the real GET before it's ever sent, and the client sees this as a network error and
retries forever. Same failure mode already documented in streaming_upload_resource.py's own
render_OPTIONS comment, just not applied here yet. Regression test for the fix.
"""

from pumice_server.watch_resource import watch_cors_options_headers


def test_echoes_the_request_origin():
    headers = dict(watch_cors_options_headers(b"https://example.com"))
    assert headers[b"Access-Control-Allow-Origin"] == b"https://example.com"


def test_allows_get_and_options():
    headers = dict(watch_cors_options_headers(b"*"))
    assert headers[b"Access-Control-Allow-Methods"] == b"GET, OPTIONS"


def test_allows_authorization_header():
    headers = dict(watch_cors_options_headers(b"*"))
    assert b"authorization" in headers[b"Access-Control-Allow-Headers"].lower()


def test_allows_credentials():
    headers = dict(watch_cors_options_headers(b"*"))
    assert headers[b"Access-Control-Allow-Credentials"] == b"true"
