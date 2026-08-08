"""Pure OAuth 2.1 authorization-code + PKCE logic for the MCP browser-auth flow (see web.py's
oauth_* views for the actual HTTP endpoints). Kept separate from web.py so the security-critical
bits -- PKCE verification, one-time/short-lived code issuance -- are testable without spinning
up Pyramid or a database.

This is deliberately NOT a general-purpose OAuth server: it implements only the subset the MCP
Authorization spec needs -- RFC 8414 authorization-server metadata, RFC 9728 protected-resource
metadata, RFC 7591 dynamic client registration, and the authorization_code grant with mandatory
PKCE (S256 only -- OAuth 2.1 drops "plain"). No refresh tokens: what this hands back as an
access_token is literally a row in the device_tokens table (see web.py's oauth_token_view), and
those don't expire, so there's nothing to refresh.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

AUTHORIZATION_CODE_TTL_SECONDS = 600  # RFC 6749's suggested authorization-code lifetime ceiling


def generate_client_id() -> str:
    return secrets.token_urlsafe(24)


def generate_authorization_code() -> str:
    return secrets.token_urlsafe(32)


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """code_challenge is base64url(SHA256(code_verifier)) with padding stripped, per RFC 7636.
    hmac.compare_digest avoids leaking timing information about a partial match."""
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)


class AuthorizationCodeStore:
    """In-memory, single-process store for issued authorization codes. Deliberately not
    persisted: a code lives for at most AUTHORIZATION_CODE_TTL_SECONDS and is consumed exactly
    once, so losing them on a server restart just means an in-flight login has to be retried --
    never a data-loss concern the way device_tokens (which must survive restarts) would be.
    """

    def __init__(self):
        self._codes: dict = {}

    def issue(self, *, username: str, client_id: str, redirect_uri: str, code_challenge: str, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        # An abandoned code -- issued but never exchanged -- would otherwise never get evicted:
        # peek() (used to validate a token request before deciding whether to consume()) never
        # deletes, so a code nobody ever finishes exchanging just sits here past its own expiry
        # forever. Sweeping here, on the comparatively rare "a new authorization just got
        # approved" path, keeps this self-cleaning without a background timer.
        expired = [c for c, entry in self._codes.items() if now > entry["expires_at"]]
        for c in expired:
            del self._codes[c]

        code = generate_authorization_code()
        self._codes[code] = {
            "username": username,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires_at": now + AUTHORIZATION_CODE_TTL_SECONDS,
        }
        return code

    def peek(self, code: str, *, now: float | None = None) -> dict | None:
        """Read-only lookup, for validating a token request (client_id/redirect_uri/PKCE)
        before deciding whether the code should actually be burned. Returns None if the code is
        unknown or expired, without deleting anything -- a *failed* validation attempt (wrong
        code_verifier, mismatched client_id, ...) must never consume the code, or a single bad
        guess/retry would permanently lock the legitimate client out of its own, otherwise-valid
        exchange. Call consume() only once every check has already passed against this."""
        entry = self._codes.get(code)
        if entry is None:
            return None
        if (now if now is not None else time.time()) > entry["expires_at"]:
            return None
        return entry

    def consume(self, code: str, *, now: float | None = None) -> dict | None:
        """Deletes and returns the entry (one-time use), or None if the code is unknown or
        expired. Always removes whatever was stored under `code` first (an expired entry is
        evicted here too, not left to leak forever, even though it also returns None) --
        deliberately NOT implemented in terms of peek(), since peek() must stay non-destructive.
        Meant to be called only after peek() + every other check has already succeeded -- see
        peek()'s docstring for why a failed check must never reach this."""
        entry = self._codes.pop(code, None)
        if entry is None:
            return None
        if (now if now is not None else time.time()) > entry["expires_at"]:
            return None
        return entry
