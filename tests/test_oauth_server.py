import base64
import hashlib

from pumice_server.oauth_server import (
    AUTHORIZATION_CODE_TTL_SECONDS,
    AuthorizationCodeStore,
    generate_authorization_code,
    generate_client_id,
    verify_pkce,
)


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_generate_client_id_and_code_are_nonempty_and_distinct():
    a, b = generate_client_id(), generate_client_id()
    assert a and b and a != b

    a, b = generate_authorization_code(), generate_authorization_code()
    assert a and b and a != b


def test_verify_pkce_accepts_matching_verifier():
    verifier = "a" * 43  # RFC 7636 minimum length
    assert verify_pkce(verifier, _challenge_for(verifier)) is True


def test_verify_pkce_rejects_wrong_verifier():
    verifier = "a" * 43
    wrong_challenge = _challenge_for("b" * 43)
    assert verify_pkce(verifier, wrong_challenge) is False


def test_verify_pkce_rejects_missing_values():
    assert verify_pkce("", "somechallenge") is False
    assert verify_pkce("someverifier", "") is False


def test_authorization_code_store_issue_then_consume():
    store = AuthorizationCodeStore()
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz")

    entry = store.consume(code)

    assert entry["username"] == "alice"
    assert entry["client_id"] == "c1"
    assert entry["redirect_uri"] == "http://localhost:1234/cb"
    assert entry["code_challenge"] == "xyz"


def test_authorization_code_store_consume_is_one_time_use():
    store = AuthorizationCodeStore()
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz")

    assert store.consume(code) is not None
    assert store.consume(code) is None


def test_authorization_code_store_consume_unknown_code_returns_none():
    store = AuthorizationCodeStore()
    assert store.consume("no-such-code") is None


def test_authorization_code_store_consume_expired_code_returns_none():
    store = AuthorizationCodeStore()
    issued_at = 1_000_000.0
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz", now=issued_at)

    past_expiry = issued_at + AUTHORIZATION_CODE_TTL_SECONDS + 1

    assert store.consume(code, now=past_expiry) is None
    # also confirms expired codes are evicted, not just rejected -- a second consume (even
    # within a hypothetically valid window) still finds nothing there
    assert store.consume(code, now=issued_at) is None


def test_authorization_code_store_peek_does_not_consume():
    store = AuthorizationCodeStore()
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz")

    first = store.peek(code)
    second = store.peek(code)

    assert first == second
    assert first["username"] == "alice"
    # still there for a real consume() after peeking it any number of times
    assert store.consume(code) is not None


def test_authorization_code_store_a_failed_check_after_peek_does_not_burn_the_code():
    # Regression test: a token request with e.g. a wrong code_verifier must be rejectable
    # without destroying the legitimate client's ability to retry with the *correct*
    # code_verifier afterwards. Simulates the real call site's pattern -- peek() to validate,
    # only consume() once every check (here: none, since this variable is bogus data) passes.
    store = AuthorizationCodeStore()
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz")

    entry = store.peek(code)
    assert entry["code_challenge"] != "some-wrong-value-a-bad-request-might-send"  # the "failed check"
    # (a real caller would return an error here instead of calling consume())

    # the legitimate client's own, correct follow-up request still succeeds
    assert store.consume(code) is not None


def test_authorization_code_store_peek_returns_none_for_unknown_or_expired_code():
    store = AuthorizationCodeStore()
    assert store.peek("no-such-code") is None

    issued_at = 1_000_000.0
    code = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz", now=issued_at)
    past_expiry = issued_at + AUTHORIZATION_CODE_TTL_SECONDS + 1
    assert store.peek(code, now=past_expiry) is None


def test_authorization_code_store_sweeps_abandoned_codes_on_issue():
    # A code that's peek()ed (never consume()d -- e.g. its flow was simply abandoned) would
    # otherwise never be evicted, since peek() is intentionally non-destructive. issue()
    # opportunistically sweeps expired entries so the store doesn't grow unbounded.
    store = AuthorizationCodeStore()
    issued_at = 1_000_000.0
    abandoned = store.issue(username="alice", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="xyz", now=issued_at)

    past_expiry = issued_at + AUTHORIZATION_CODE_TTL_SECONDS + 1
    assert store.peek(abandoned, now=past_expiry) is None  # expired, but peek() never deletes
    assert abandoned in store._codes  # still physically present after only a peek

    store.issue(username="bob", client_id="c1", redirect_uri="http://localhost:1234/cb", code_challenge="abc", now=past_expiry)

    assert abandoned not in store._codes
