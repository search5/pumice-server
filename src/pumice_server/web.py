import time
import json
import logging
import re
import os
import uuid
from babel.messages.pofile import read_po
from babel.support import Translations
from urllib.parse import quote, unquote
from pyramid.config import Configurator
from pyramid.response import Response
from pyramid.view import view_config

from .plugin_registry import discover_plugins
from .plugin_loader import load_plugin_autofill_paths, load_plugin_config_fields, run_enabled_plugins, suggest_plugin_autofill
from .plugin_config_crypto import PluginConfigEncryptionError, decrypt_json, encrypt_json
from .plugin_resolve_disk_cache import make_disk_cache
from .plugin_autofill import read_vault_files, sanitize_autofill_suggestions
from . import oauth_server

logger = logging.getLogger("pumice_server.web")

# ─── i18n ────────────────────────────────────────────────────────────────
# Locale is negotiated per-request from the browser's Accept-Language header --
# no client-side language detection needed. Translation text lives in gettext
# catalogs (locale/{lang}/LC_MESSAGES/messages.po, compiled to .mo) rather than
# hand-rolled JSON -- msgids are the same dotted keys the rest of this codebase
# (and the client-side JS I18N blob) already address strings by, e.g.
# "login.button_submit", not the English source text itself; that keeps every
# existing t('some.key', lang) call site and the client's dotted-path traversal
# of the I18N blob unchanged, while the underlying storage/lookup becomes the
# standard Python gettext/Babel toolchain (regenerate with
# scripts/compile_locale.py after editing a .po file by hand).
#
# babel.support.Translations wraps stdlib gettext -- a fresh instance is loaded
# per locale and cached, rather than mutating shared global state, since this
# server handles requests concurrently (Twisted + asyncio) and a global
# "current locale" would race across requests.
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
_DOMAIN = "messages"
_SUPPORTED_LOCALES = ["ko", "en"]
_FALLBACK_LOCALE = "en"
_I18N_PARAM_RE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")

_TRANSLATIONS_CACHE: dict = {}

def _translations_for(lang: str) -> Translations:
    if lang not in _TRANSLATIONS_CACHE:
        # `locales` is a fallback chain, most-preferred first -- a msgid missing from `lang`'s
        # own catalog (shouldn't happen; every key is expected to exist in every locale) falls
        # through to the fallback locale instead of surfacing the raw key to a user.
        _TRANSLATIONS_CACHE[lang] = Translations.load(_LOCALE_DIR, [lang, _FALLBACK_LOCALE], domain=_DOMAIN)
    return _TRANSLATIONS_CACHE[lang]

def detect_lang(request) -> str:
    return request.accept_language.best_match(_SUPPORTED_LOCALES, default_match=_FALLBACK_LOCALE)

def t(key: str, lang: str, **params) -> str:
    text = _translations_for(lang).gettext(key)
    if params:
        text = _I18N_PARAM_RE.sub(lambda m: str(params.get(m.group(1), "")), text)
    return text

# The full set of msgids, read once from the fallback locale's own .po source (every key is
# expected to exist there, by construction) -- gettext catalogs are a flat msgid->msgstr
# mapping with no way to "list all keys" via the public Translations/gettext API, so this reads
# the human-edited .po directly instead of the compiled .mo.
def _all_keys() -> list:
    po_path = os.path.join(_LOCALE_DIR, _FALLBACK_LOCALE, "LC_MESSAGES", f"{_DOMAIN}.po")
    with open(po_path, "r", encoding="utf-8") as f:
        catalog = read_po(f, locale=_FALLBACK_LOCALE, domain=_DOMAIN)
    return [message.id for message in catalog if message.id]

_ALL_KEYS = None

# Raw locale dicts, loaded once and cached, for embedding a fully-resolved translation blob
# into a page's <script> for the handful of strings that JS generates at runtime (toasts,
# confirm dialogs, fetch-rendered table rows). Reconstructs the nested {"login": {"button_submit":
# ...}} shape the client's dotted-path traversal expects from the flat gettext catalog.
_LOCALE_DATA_CACHE: dict = {}

def get_locale_dict(lang: str) -> dict:
    global _ALL_KEYS
    if lang not in _LOCALE_DATA_CACHE:
        if _ALL_KEYS is None:
            _ALL_KEYS = _all_keys()
        nested: dict = {}
        for key in _ALL_KEYS:
            node = nested
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = t(key, lang)
        _LOCALE_DATA_CACHE[lang] = nested
    return _LOCALE_DATA_CACHE[lang]

# Pyramid tween that allows CORS
def cors_tween_factory(handler, registry):
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, obs-token, obs-id, obs-path, obs-hash, X-Device-Name, X-User-Name, x-grpc-web, x-user-agent, grpc-timeout",
        "Access-Control-Expose-Headers": "grpc-status, grpc-message",
        "Access-Control-Max-Age": "86400",
    }
    def cors_tween(request):
        if request.method == "OPTIONS":
            response = Response(status=200)
            response.headers.update(cors_headers)
            return response
        response = handler(request)
        response.headers.update(cors_headers)
        return response
    return cors_tween

# Pyramid tween (middleware) that verifies the Authorization Bearer token
# There's exactly one kind of credential in the whole system: a device_tokens row (see
# repository.py). It authenticates gRPC sync calls (checked directly in service.py) *and* every
# HTTP request here -- the browser dashboard included, via a session cookie of the same token
# rather than a copy-pasted "web token" (which used to be a separate users.token column/concept;
# removed, since nothing but the dashboard itself ever consumed it and cookies are the natural
# fit for a browser session anyway).
SESSION_COOKIE_NAME = "session_token"

def extract_token(request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    obs_token = request.headers.get("obs-token", "")
    if obs_token:
        return obs_token.strip()
    # A body token for APIs that take it in the JSON body (e.g. unpublish)
    try:
        if request.body:
            body_token = request.json_body.get("token")
            if body_token:
                return body_token.strip()
    except Exception:
        pass
    return request.cookies.get(SESSION_COOKIE_NAME, "")

def set_session_cookie(request, token: str) -> None:
    request.response.set_cookie(
        SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="Lax",
        secure=(request.scheme == "https"),
        path="/",
        max_age=60 * 60 * 24 * 365,  # individual sessions are revoked via device management, not expiry
    )

def clear_session_cookie(request) -> None:
    request.response.delete_cookie(SESSION_COOKIE_NAME, path="/")

# ─── Authorization (Pyramid ACL) ────────────────────────────────────────────
# Permission checking used to be ad hoc: every view called get_authenticated_user()/
# verify_vault_ownership() by hand and returned its own 401/403 Response. That's now
# centralized into a real Pyramid security policy + ACLs, so views just declare
# `permission='authenticated' | 'admin' | 'vault-access'` on @view_config and never see an
# unauthorized caller at all -- Pyramid raises HTTPForbidden before the view body runs, and
# forbidden_view_api()/forbidden_view_page() below split on path_info to reproduce the old
# 401 (no identity) / 403 (wrong identity) JSON shape for /api/* while redirecting browser
# page navigation to /login instead.
from pyramid.authorization import Allow, Authenticated, Everyone, ACLHelper
from pyramid.security import NO_PERMISSION_REQUIRED
from pyramid.view import forbidden_view_config
from pyramid.httpexceptions import HTTPForbidden, HTTPFound

class DeviceTokenSecurityPolicy:
    def identity(self, request):
        token = extract_token(request)
        if not token or not request.repository:
            return None
        device = request.repository.get_device_token(token)
        if not device:
            return None
        admin_user = os.getenv("ADMIN_USER")
        is_admin = (device["username"] == admin_user) if admin_user else False
        return {"username": device["username"], "is_admin": is_admin}

    def authenticated_userid(self, request):
        identity = request.identity
        return identity["username"] if identity else None

    def forget(self, request, **kw):
        return []

    def remember(self, request, userid, **kw):
        return []

    def permits(self, request, context, permission):
        identity = request.identity
        principals = [Everyone]
        if identity is not None:
            principals.append(Authenticated)
            principals.append(f"user:{identity['username']}")
            if identity["is_admin"]:
                principals.append("role:admin")
        acl = getattr(context, "__acl__", [])
        return ACLHelper().permits(context, principals, permission)

class RootFactory:
    __acl__ = [
        (Allow, Authenticated, "authenticated"),
        (Allow, "role:admin", "admin"),
    ]
    def __init__(self, request):
        pass

def _extract_vault_id(request) -> str:
    if request.matchdict:
        vault_id = request.matchdict.get("vault_id")
        if vault_id:
            return vault_id
    vault_id = request.params.get("vault_id")
    if vault_id:
        return vault_id
    vault_id = request.headers.get("obs-id")
    if vault_id:
        return vault_id
    try:
        if request.body:
            body = request.json_body
            return body.get("vault_id") or body.get("id") or body.get("site_uid") or ""
    except Exception:
        pass
    return ""

class VaultContext:
    """Route factory for every endpoint scoped to a single vault. Resolves the vault_id from
    whichever of matchdict/params/obs-id header/JSON body the calling convention uses (see
    _extract_vault_id). A vault's true identity is (owner_username, vault_id), and
    owner_username always comes from the caller's own authenticated identity, never from client
    input -- so there's no cross-account vault access to check: a caller can only ever address
    their own vaults. (An earlier version of this checked vault_id against a global
    vault_id -> owner registry and let the first caller "claim" an unclaimed vault_id -- removed,
    because vault_id alone isn't globally unique: "Obsidian Vault" is Obsidian's own default vault
    name, so two different accounts' same-named vaults collided and whichever synced first
    permanently locked the other out.)"""

    def __init__(self, request):
        self.vault_id = _extract_vault_id(request)
        identity = request.identity
        self.owner = identity["username"] if identity else None
        self.__acl__ = [(Allow, f"user:{self.owner}", "vault-access")] if self.owner else []

@forbidden_view_config(path_info=r'^/api/', renderer="json")
def forbidden_view_api(request):
    # Same 401-vs-403 distinction the manual per-view checks used to make: no identity at all
    # is "you need to log in" (401), a real identity that just isn't allowed here is "you're
    # logged in as the wrong person" (403). Every /api/* caller (Obsidian plugin and the
    # dashboard's own fetch() calls alike) expects JSON here, never a redirect -- fetch()
    # follows redirects transparently and would hand the caller /login's HTML instead of an
    # error it can act on.
    status = 401 if request.identity is None else 403
    request.response.status = status
    return {"error": "Unauthorized" if status == 401 else "Forbidden"}

@forbidden_view_config()
def forbidden_view_page(request):
    # Catch-all for everything outside /api/* -- real browser navigation, which can just be
    # redirected to the login page instead of being handed a JSON error body.
    return HTTPFound(location="/login")

def get_authenticated_user(request):
    token = extract_token(request)
    if not token:
        return None

    device = request.repository.get_device_token(token) if request.repository else None
    if device:
        admin_user = os.getenv("ADMIN_USER")
        is_admin = (device["username"] == admin_user) if admin_user else False
        return {"username": device["username"], "is_admin": is_admin}

    return None

# Helper that resolves a vault's absolute physical storage path. Nested under owner_username for
# the same reason the DB tables are keyed by (owner_username, vault_id) now -- vault_id alone
# ("Obsidian Vault" being Obsidian's own default name) isn't unique across accounts.
def get_vault_path(data_dir: str, owner_username: str, vault_id: str) -> str:
    vaults_dir = os.path.join(data_dir, "vaults", owner_username)
    vault_path = os.path.abspath(os.path.join(vaults_dir, vault_id))
    if not vault_path.startswith(os.path.abspath(vaults_dir)):
        raise ValueError("Invalid vault ID")
    return vault_path

# Helper that resolves the publish metadata directory
def get_publish_meta_dir(data_dir: str, owner_username: str, vault_id: str) -> str:
    d = os.path.join(data_dir, "publish_meta", owner_username, vault_id)
    os.makedirs(d, exist_ok=True)
    return d

def load_publish_meta(data_dir: str, owner_username: str, vault_id: str, filename: str, default=None):
    meta_dir = get_publish_meta_dir(data_dir, owner_username, vault_id)
    path = os.path.join(meta_dir, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

def save_publish_meta(data_dir: str, owner_username: str, vault_id: str, filename: str, data):
    meta_dir = get_publish_meta_dir(data_dir, owner_username, vault_id)
    path = os.path.join(meta_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

@view_config(route_name='root', permission='authenticated')
def root_view(request):
    # Only ever reached with a real, valid identity -- an unauthenticated caller never gets
    # here at all, forbidden_view_page redirects them to /login before this view body runs.
    return HTTPFound(location="/dashboard")

@view_config(route_name='ping', renderer='json', permission=NO_PERMISSION_REQUIRED)
def ping_view(request):
    return {
        "status": "ok",
        "message": "Obsidian Sync HTTP Portal (Pyramid) is running on Twisted reactor",
        "timestamp_ms": int(time.time() * 1000)
    }

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

def _static_version(filename: str) -> str:
    # A static <script src> with no cache-busting means a browser that already cached an old
    # copy can keep using it indefinitely (Last-Modified alone doesn't force revalidation) --
    # this file's own mtime as a query param means the URL only changes when the file actually
    # does, so a real edit is always picked up on the next page load without needing a hard
    # refresh, while an unchanged file still gets cached normally.
    try:
        return str(int(os.path.getmtime(os.path.join(_STATIC_DIR, filename))))
    except OSError:
        return "0"

@view_config(route_name='login_page', permission=NO_PERMISSION_REQUIRED, renderer='templates/login.jinja2')
def login_page_view(request):
    lang = detect_lang(request)
    return {
        'lang': lang,
        't': lambda key, **params: t(key, lang, **params),
        # The '</' guard keeps a translation string from ever prematurely closing this inline
        # <script> tag (e.g. a value containing "</script>") -- there's no other untrusted input
        # here, translations are our own locale/*.json files, but this is free insurance.
        'i18n_json': json.dumps(get_locale_dict(lang), ensure_ascii=False).replace('</', '<\\/'),
        'login_js_version': _static_version('login.js'),
    }

@view_config(route_name='dashboard_page', permission=NO_PERMISSION_REQUIRED, renderer='templates/dashboard.jinja2')
def dashboard_page_view(request):
    lang = detect_lang(request)
    return {
        'lang': lang,
        't': lambda key, **params: t(key, lang, **params),
        # See login_page_view's identical comment -- same '</' guard, same reasoning.
        'i18n_json': json.dumps(get_locale_dict(lang), ensure_ascii=False).replace('</', '<\\/'),
        'dashboard_js_version': _static_version('dashboard.js'),
    }

@view_config(route_name='admin_vaults', renderer='json', permission='authenticated')
def admin_vaults_view(request):
    # No admin bypass, intentionally: even the existence/name of another user's vault isn't
    # admin's to see, only their own.
    vaults = request.repository.get_vaults_by_owner(request.identity["username"])
    return {"vaults": vaults}

@view_config(route_name='admin_vault_files', renderer='json', permission='vault-access')
def admin_vault_files_view(request):
    vault_id = request.context.vault_id
    files_meta = request.repository.load_all(request.context.owner, vault_id)
    files_list = []
    for path, meta in files_meta.items():
        files_list.append({
            "path": path,
            "modified_at_ms": meta.get("modified_at_ms", 0),
            "size_bytes": meta.get("size_bytes", 0),
            "is_deleted": meta.get("is_deleted", False),
            "content_hash": meta.get("content_hash", "")
        })
    return {"files": files_list}

@view_config(route_name='admin_published', renderer='json', permission='authenticated')
def admin_published_view(request):
    user_info = request.identity
    data_dir = request.registry.settings.get("data_dir")
    pub_dir = os.path.join(data_dir, "published")
    sites = []
    if os.path.exists(pub_dir):
        for user_dir in os.listdir(pub_dir):
            user_path = os.path.join(pub_dir, user_dir)
            if os.path.isdir(user_path):
                for item in os.listdir(user_path):
                    if os.path.isdir(os.path.join(user_path, item)):
                        # Admin sees every published site here -- unlike private vault content,
                        # published sites are already publicly viewable on the web, so there's
                        # nothing extra being exposed by this listing. The directory layout
                        # (published/{owner}/{vault_id}) already encodes ownership, so no extra
                        # lookup is needed for the non-admin case either.
                        if user_info["is_admin"] or user_dir == user_info["username"]:
                            sites.append({"id": item, "username": user_dir})
    return {"published_sites": sites}

import hashlib
import secrets

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt.hex() + ":" + key.hex()

def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return key == new_key
    except Exception:
        return False

@view_config(route_name='user_login', renderer='json', permission=NO_PERMISSION_REQUIRED)
def user_login_view(request):
    try:
        body = request.json_body
        username = body.get("username") or body.get("email")
        password = body.get("password") or body.get("pw") or ""

        if not username:
            return Response(body=json.dumps({"error": "Missing username or email"}).encode("utf-8"), status=400, content_type="application/json")
            
        device_name = body.get("device_name") or "Unknown Device"

        # Check for the env-var-based admin account
        admin_user = os.getenv("ADMIN_USER")
        admin_password = os.getenv("ADMIN_PASSWORD")
        if admin_user and admin_password and username == admin_user:
            if password == admin_password:
                # One device_tokens row per login -- it's the only credential in the system now.
                # The browser gets it as an HttpOnly cookie; it's also returned in the JSON body
                # so the Obsidian "Log in" deep-link flow can hand it off via the
                # obsidian://pumice-auth redirect. Logging in again from another
                # device/browser doesn't invalidate earlier sessions -- each is its own row,
                # individually revocable from the device management UI.
                session_token = secrets.token_hex(32)
                request.repository.create_device_token(session_token, admin_user, device_name, int(time.time() * 1000))
                set_session_cookie(request, session_token)
                logger.info(f"Successful admin login for user '{username}' via environment variables.")
                return {
                    "device_token": session_token,
                    "email": admin_user,
                    "name": "Administrator",
                    "username": admin_user,
                    "is_admin": True
                }
            else:
                logger.warning(f"Failed admin login attempt for user '{username}': password mismatch.")
                return Response(body=json.dumps({"error": "Invalid email or password"}).encode("utf-8"), status=401, content_type="application/json")

        # Look up the user in the DB -- accounts are provisioned by an admin only
        # (POST /api/admin/users/create), no self-service signup here.
        user = request.repository.get_user_by_username(username)

        if not user:
            logger.warning(f"Login attempt for unknown user '{username}'.")
            return Response(body=json.dumps({"error": "Invalid email or password"}).encode("utf-8"), status=401, content_type="application/json")

        pw_hash = user["password_hash"]
        if not verify_password(password, pw_hash):
            logger.warning(f"Failed login attempt for user '{username}': password mismatch.")
            return Response(body=json.dumps({"error": "Invalid email or password"}).encode("utf-8"), status=401, content_type="application/json")

        logger.info(f"Successful login for user '{username}'.")

        session_token = secrets.token_hex(32)
        request.repository.create_device_token(session_token, username, device_name, int(time.time() * 1000))
        set_session_cookie(request, session_token)

        return {
            "device_token": session_token,
            "username": user.get("username"),
            "email": user.get("email") or user.get("username"),
            "name": user.get("name") or user.get("username").split("@")[0],
            "is_admin": bool(user.get("is_admin", False))
        }
    except Exception as e:
        logger.error(f"User login process failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='get_token_info', renderer='json', permission=NO_PERMISSION_REQUIRED)
def get_token_info(request):
    # Deliberately public/tolerant of a missing or invalid token -- callers use this to probe
    # whether their current credential is still good (authorized: false is a normal response,
    # not an error). When it IS valid, the client builds the publish site URL
    # (/publish/{username}/{vault}/...) using the username the server actually recognizes, not
    # the userName setting (a free-text display label the user can type anything into), or the
    # path won't line up.
    user_info = request.identity
    return {
        "authorized": user_info is not None,
        "username": user_info["username"] if user_info else None,
        "database_connected": request.repository is not None,
        "server_time": int(time.time() * 1000)
    }

# ─── OAuth 2.1 authorization server for the MCP browser-auth flow ──────────────────────────
# Implements exactly the subset the MCP Authorization spec needs so an MCP client (Claude
# Code/Desktop, ...) can go from "just the server URL" to a working credential without the user
# ever manually creating/copying a device token: RFC 8414 authorization-server metadata, RFC
# 9728 protected-resource metadata, RFC 7591 dynamic client registration, and the
# authorization_code + PKCE grant. See oauth_server.py for the actual crypto/state logic; these
# views are thin HTTP glue over it. What comes out the other end of a successful flow is a
# perfectly ordinary device_token row (create_device_token -- the same thing the dashboard's
# "create a new device" button and a real username+password login both produce) -- OAuth here
# is only the browser-consent wrapper around minting one, not a parallel credential system, and
# nothing about /mcp's own auth check changes at all.

_authorization_codes = oauth_server.AuthorizationCodeStore()

def _oauth_issuer(request) -> str:
    return f"{request.scheme}://{request.host}"

@view_config(route_name='oauth_protected_resource_metadata', renderer='json', permission=NO_PERMISSION_REQUIRED)
def oauth_protected_resource_metadata_view(request):
    issuer = _oauth_issuer(request)
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
    }

@view_config(route_name='oauth_authorization_server_metadata', renderer='json', permission=NO_PERMISSION_REQUIRED)
def oauth_authorization_server_metadata_view(request):
    issuer = _oauth_issuer(request)
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }

@view_config(route_name='oauth_register', renderer='json', request_method='POST', permission=NO_PERMISSION_REQUIRED)
def oauth_register_view(request):
    # Public clients only (PKCE, no client_secret) -- an MCP client self-registers here the
    # first time it sees this server, with no admin action and nothing secret handed back.
    try:
        body = request.json_body
    except Exception:
        body = {}

    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return Response(status=400, body=json.dumps({"error": "invalid_client_metadata", "error_description": "redirect_uris is required"}).encode("utf-8"), content_type="application/json")

    client_id = oauth_server.generate_client_id()
    client_name = body.get("client_name") or "Unnamed MCP client"
    request.repository.create_oauth_client(client_id, client_name, redirect_uris, int(time.time() * 1000))
    logger.info(f"OAuth client registered: '{client_name}' ({client_id})")
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }

def _validate_authorize_request(repository, client_id, redirect_uri, response_type, code_challenge, code_challenge_method):
    if response_type != "code":
        return "unsupported_response_type"
    if code_challenge_method != "S256":
        return "invalid_request: code_challenge_method must be S256"
    if not code_challenge:
        return "invalid_request: missing code_challenge"
    if not client_id:
        return "invalid_request: missing client_id"
    client = repository.get_oauth_client(client_id)
    if not client:
        return "invalid_client"
    if redirect_uri not in client["redirect_uris"]:
        return "invalid_request: redirect_uri not registered for this client"
    return None

@view_config(route_name='oauth_authorize', request_method='GET', renderer='templates/oauth_authorize.jinja2', permission=NO_PERMISSION_REQUIRED)
def oauth_authorize_view(request):
    # Not logged in yet: bounce through the ordinary login page, same session cookie as
    # everything else, then come straight back here with every original query param intact.
    if request.identity is None:
        return HTTPFound(location="/login?next=" + quote(request.url, safe=""))

    lang = detect_lang(request)
    client_id = request.params.get("client_id")
    redirect_uri = request.params.get("redirect_uri")
    response_type = request.params.get("response_type")
    code_challenge = request.params.get("code_challenge")
    code_challenge_method = request.params.get("code_challenge_method")
    state = request.params.get("state", "")

    error = _validate_authorize_request(request.repository, client_id, redirect_uri, response_type, code_challenge, code_challenge_method)
    if error:
        return Response(status=400, body=error, content_type="text/plain")

    client = request.repository.get_oauth_client(client_id)
    return {
        'lang': lang, 't': lambda key, **params: t(key, lang, **params),
        'client_name': client['client_name'],
        'username': request.identity['username'],
        'client_id': client_id, 'redirect_uri': redirect_uri,
        'code_challenge': code_challenge, 'state': state,
    }

@view_config(route_name='oauth_authorize', request_method='POST', permission='authenticated')
def oauth_authorize_submit_view(request):
    username = request.identity['username']
    client_id = request.POST.get("client_id")
    redirect_uri = request.POST.get("redirect_uri")
    code_challenge = request.POST.get("code_challenge")
    state = request.POST.get("state", "")
    approved = request.POST.get("approve") == "1"

    client = request.repository.get_oauth_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        return Response(status=400, body="invalid_request")

    if not approved:
        location = f"{redirect_uri}?error=access_denied" + (f"&state={quote(state)}" if state else "")
        return HTTPFound(location=location)

    code = _authorization_codes.issue(username=username, client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge)
    logger.info(f"User '{username}' approved OAuth client '{client_id}'.")
    location = f"{redirect_uri}?code={quote(code)}" + (f"&state={quote(state)}" if state else "")
    return HTTPFound(location=location)

@view_config(route_name='oauth_token', renderer='json', request_method='POST', permission=NO_PERMISSION_REQUIRED)
def oauth_token_view(request):
    # RFC 6749: token requests are application/x-www-form-urlencoded, not JSON -- request.POST
    # (not json_body) is the correct read here.
    params = request.POST
    if params.get("grant_type") != "authorization_code":
        return Response(status=400, body=json.dumps({"error": "unsupported_grant_type"}).encode("utf-8"), content_type="application/json")

    # Validate everything against a read-only peek() first -- only a fully successful request
    # is allowed to actually burn the code (consume(), below). A wrong code_verifier or
    # mismatched client_id/redirect_uri must fail without invalidating the code, or a single
    # bad guess/retry would permanently lock the legitimate client out of its own valid exchange.
    code = params.get("code")
    entry = _authorization_codes.peek(code) if code else None
    if not entry:
        return Response(status=400, body=json.dumps({"error": "invalid_grant"}).encode("utf-8"), content_type="application/json")

    if entry["client_id"] != params.get("client_id") or entry["redirect_uri"] != params.get("redirect_uri"):
        return Response(status=400, body=json.dumps({"error": "invalid_grant"}).encode("utf-8"), content_type="application/json")

    if not oauth_server.verify_pkce(params.get("code_verifier") or "", entry["code_challenge"]):
        return Response(status=400, body=json.dumps({"error": "invalid_grant", "error_description": "PKCE verification failed"}).encode("utf-8"), content_type="application/json")

    _authorization_codes.consume(code)

    client = request.repository.get_oauth_client(entry["client_id"])
    device_name = (client["client_name"] if client else None) or "OAuth client"

    access_token = secrets.token_hex(32)
    request.repository.create_device_token(access_token, entry["username"], device_name, int(time.time() * 1000))
    logger.info(f"OAuth token issued for user '{entry['username']}' via client '{entry['client_id']}'.")

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }

FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)

# YAML frontmatter holding the publish flag and similar fields isn't content meant for site visitors
# — it's metadata used only on our (client) side to decide what's eligible for publishing, so it
# gets stripped before rendering. The core markdown library doesn't understand frontmatter, so left
# alone the "---" would render as an <hr> with its contents exposed as an ordinary paragraph.
def strip_frontmatter(md_content: str) -> str:
    return FRONTMATTER_RE.sub("", md_content, count=1)

def parse_wikilinks(text: str, username: str, vault_id: str) -> str:
    # 1. Handle image wikilinks: ![[image.png|width]]
    def repl_img(match):
        target = match.group(1).strip()
        safe_target = quote(target, safe='/')
        src = f"/publish/{username}/{vault_id}/{safe_target}"
        width_attr = ""
        if match.group(2):
            width = match.group(2).strip()
            if width.isdigit():
                width_attr = f' width="{width}"'
        return f'<img src="{src}" alt="{target}"{width_attr}>'
        
    text = re.sub(r'!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', repl_img, text)

    # 2. Handle regular document wikilinks: [[target|label]] or [[target]]
    def repl_link(match):
        target = match.group(1).strip()
        label = match.group(2).strip() if match.group(2) else target
        
        base_target, ext = os.path.splitext(target)
        if ext.lower() == ".md":
            url_target = base_target
        else:
            url_target = target
            
        safe_target = quote(url_target, safe='/')
        href = f"/publish/{username}/{vault_id}/{safe_target}"
        return f'<a href="{href}" class="wiki-link">{label}</a>'
        
    text = re.sub(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', repl_link, text)
    return text

@view_config(route_name='publish_list', renderer='json', permission='vault-access')
def publish_list_view(request):
    try:
        vault_id = request.context.vault_id
        files = request.repository.get_published_files(request.context.owner, vault_id)

        data_dir = request.registry.settings.get("data_dir")
        published_base = os.path.join(data_dir, "published", request.context.owner, vault_id)

        enriched_files = []
        for f in files:
            file_path = os.path.join(published_base, f["path"])
            ctime_ms = 0
            mtime_ms = 0
            size = 0
            try:
                stat = os.stat(file_path)
                ctime_ms = int(stat.st_ctime * 1000)
                mtime_ms = int(stat.st_mtime * 1000)
                size = stat.st_size
            except Exception:
                pass
            enriched_files.append({
                "path": f["path"],
                "hash": f.get("hash", ""),
                "ctime": ctime_ms,
                "mtime": mtime_ms,
                "size": size,
            })

        return {"files": enriched_files, "owner": True}
    except Exception as e:
        logger.error(f"HTTP publish_list failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_upload', renderer='json', permission='vault-access')
def publish_upload_view(request):
    try:
        vault_id = request.context.vault_id
        obs_path_enc = request.headers.get("obs-path")
        obs_hash = request.headers.get("obs-hash")

        if not obs_path_enc or not obs_hash:
            return Response(body=json.dumps({"error": "Missing obs-id, obs-path or obs-hash header"}).encode("utf-8"), status=400, content_type="application/json")

        path = unquote(obs_path_enc)

        # File storage location: data_dir/published/username/vault_id/path
        data_dir = request.registry.settings.get("data_dir")
        username = request.context.owner
        published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))
        dest_file_path = os.path.abspath(os.path.join(published_base, path))
        
        if not dest_file_path.startswith(published_base):
            return Response(body=json.dumps({"error": "Path traversal detected"}).encode("utf-8"), status=400, content_type="application/json")
            
        os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
        
        body_data = request.body
        with open(dest_file_path, "wb") as f:
            f.write(body_data)
            
        request.repository.add_published_file(request.context.owner, vault_id, path, obs_hash)
        logger.info(f"Published file saved: vault={vault_id}, path={path}, hash={obs_hash}")

        _warm_plugin_resolve_cache(request, username, vault_id, dest_file_path, data_dir)

        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_upload failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

def _warm_plugin_resolve_cache(request, owner: str, vault_id: str, file_path: str, data_dir: str) -> None:
    """Eagerly resolves this note's enabled plugins right after upload and persists the result
    to the disk cache (see publish_view/plugin_resolve_disk_cache.py), so a freshly-published
    note's first real visitor doesn't have to wait on -- or risk a slow/down -- external
    dependency a plugin calls out to (e.g. pumice-plugin-immich fetching a photo from Immich).
    Purely a latency optimization: the same cache already gets populated lazily at view time on
    a miss, so anything going wrong here (plugin down, disk error, whatever) must never fail the
    upload itself -- worst case, the first view just resolves it live instead, same as if this
    warming step didn't run at all.
    """
    try:
        if os.path.splitext(file_path)[1].lower() in _PUBLISH_MEDIA_MIME_TYPES:
            return

        enabled_plugin_ids = [p["plugin_id"] for p in request.repository.list_enabled_plugins(owner, vault_id)]
        if not enabled_plugin_ids:
            return

        with open(file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        processed_md = parse_wikilinks(strip_frontmatter(md_content), owner, vault_id)
        plugin_configs = _load_decrypted_plugin_configs(request, owner, vault_id, enabled_plugin_ids)
        cache_get, cache_set = make_disk_cache(data_dir)
        run_enabled_plugins(
            processed_md, enabled_plugin_ids, owner_username=owner, vault_id=vault_id, plugin_configs=plugin_configs,
            cache_get=cache_get, cache_set=cache_set,
        )
    except Exception as e:
        logger.error(f"Failed to eagerly warm plugin resolve cache for vault '{vault_id}': {e}")

@view_config(route_name='publish_remove', renderer='json', permission='vault-access')
def publish_remove_view(request):
    try:
        # Obsidian Publish format: JSON body {path, id, token}
        body = request.json_body
        path = body.get("path")
        vault_id = request.context.vault_id
        if not path:
            return Response(body=json.dumps({"error": "Missing path in body"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        username = request.context.owner
        published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))
        dest_file_path = os.path.abspath(os.path.join(published_base, path))
        
        if dest_file_path.startswith(published_base) and os.path.exists(dest_file_path):
            os.remove(dest_file_path)
            try:
                parent = os.path.dirname(dest_file_path)
                while parent != published_base:
                    if not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
            except Exception:
                pass
                
        request.repository.remove_published_file(request.context.owner, vault_id, path)
        logger.info(f"Published file removed: vault={vault_id}, path={path}")
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_remove failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_download', permission='vault-access')
def publish_download_view(request):
    try:
        body = request.json_body
        vault_id = request.context.vault_id
        file_path = body.get("path")
        if not file_path:
            return Response("Missing id or path", status=400)

        data_dir = request.registry.settings.get("data_dir")
        username = request.context.owner
        published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))
        dest_file_path = os.path.abspath(os.path.join(published_base, file_path))

        if not dest_file_path.startswith(published_base):
            return Response("Forbidden", status=403)

        if not os.path.exists(dest_file_path):
            return Response("Not found", status=404)

        with open(dest_file_path, "rb") as f:
            data = f.read()

        return Response(body=data, content_type="application/octet-stream")
    except Exception as e:
        logger.error(f"HTTP publish_download failed: {e}")
        return Response(str(e), status=500)

_PUBLISH_MEDIA_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}

def _published_file_hrefs(username: str, vault_id: str, files: list) -> dict:
    return {
        f: f'/publish/{quote(username, safe="")}/{quote(vault_id, safe="")}/{quote(f, safe="/")}'
        for f in files
    }

def _load_decrypted_plugin_configs(request, owner: str, vault_id: str, enabled_plugin_ids: list) -> dict:
    """{plugin_id: {entry_id: decrypted_config_dict}} for every plugin in enabled_plugin_ids --
    the shape run_enabled_plugins()'s plugin_configs expects. Shared between publish_view (live
    render) and publish_upload_view (eager cache warm on upload) so there's one place that knows
    how a plugin's stored config gets decrypted."""
    plugin_configs = {}
    for plugin_id in enabled_plugin_ids:
        entries = {}
        for entry_id, row in request.repository.get_plugin_config_entries(owner, vault_id, plugin_id).items():
            try:
                entries[entry_id] = decrypt_json(row["config_blob_b64"])
            except PluginConfigEncryptionError as e:
                logger.error(f"Failed to decrypt config entry '{entry_id}' for plugin '{plugin_id}' on vault '{vault_id}': {e}")
        plugin_configs[plugin_id] = entries
    return plugin_configs

@view_config(route_name='publish_view', permission=NO_PERMISSION_REQUIRED, renderer='templates/publish_page.jinja2')
def publish_view(request):
    username = request.matchdict.get("username")
    vault_id = request.matchdict.get("vault_id")
    path = request.matchdict.get("path", "")
    lang = detect_lang(request)

    data_dir = request.registry.settings.get("data_dir")
    published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))

    # Root: show the list of published files. `files=None` is how the template tells this
    # branch apart from an actual note's rendered content -- an empty *list* still means "show
    # the (empty) file list", not "render a note".
    if not path:
        files = []
        if os.path.isdir(published_base):
            for root, _, fnames in os.walk(published_base):
                for fname in fnames:
                    rel = os.path.relpath(os.path.join(root, fname), published_base)
                    files.append(rel.replace(os.sep, "/"))
        files = sorted(files)
        return {
            "lang": lang, "t": lambda key, **params: t(key, lang, **params),
            "username": username, "vault_id": vault_id, "title": "",
            "files": files, "file_hrefs": _published_file_hrefs(username, vault_id, files),
            "content": None,
        }

    file_path = os.path.abspath(os.path.join(published_base, path))

    if not file_path.startswith(published_base):
        return Response("Forbidden", status=403)

    if not os.path.exists(file_path):
        if not os.path.splitext(file_path)[1]:
            alt_path = file_path + ".md"
            if os.path.exists(alt_path):
                file_path = alt_path
            else:
                return Response(f"Page not found: {path}", status=404)
        else:
            return Response(f"Page not found: {path}", status=404)
            
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    if ext in _PUBLISH_MEDIA_MIME_TYPES:
        content_type = _PUBLISH_MEDIA_MIME_TYPES.get(ext, "application/octet-stream")
        with open(file_path, "rb") as f:
            return Response(body=f.read(), content_type=content_type)
            
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
    except Exception as e:
        return Response(f"Failed to read file: {e}", status=500)
        
    processed_md = parse_wikilinks(strip_frontmatter(md_content), username, vault_id)

    enabled_plugin_ids = [p["plugin_id"] for p in request.repository.list_enabled_plugins(username, vault_id)]
    if enabled_plugin_ids:
        plugin_configs = _load_decrypted_plugin_configs(request, username, vault_id, enabled_plugin_ids)
        cache_get, cache_set = make_disk_cache(data_dir)
        processed_md = run_enabled_plugins(
            processed_md, enabled_plugin_ids, owner_username=username, vault_id=vault_id, plugin_configs=plugin_configs,
            cache_get=cache_get, cache_set=cache_set,
        )

    import markdown
    html_content = markdown.markdown(processed_md, extensions=['extra', 'codehilite', 'toc'])

    title = os.path.basename(path)
    if title.endswith(".md"):
        title = title[:-3]

    return {
        "lang": lang, "t": lambda key, **params: t(key, lang, **params),
        "username": username, "vault_id": vault_id, "title": title,
        "files": None, "file_hrefs": {}, "content": html_content,
    }


@view_config(route_name='publish_slugs', renderer='json', permission='authenticated')
def publish_slugs_view(request):
    try:
        body = request.json_body
        ids = body.get("ids", [])
        owner = request.identity["username"]
        data_dir = request.registry.settings.get("data_dir")
        result = {}
        for vault_id in ids:
            site = load_publish_meta(data_dir, owner, vault_id, "site.json", {})
            result[vault_id] = site.get("slug", vault_id)
        return result
    except Exception as e:
        logger.error(f"HTTP publish_slugs failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_site', renderer='json', permission=NO_PERMISSION_REQUIRED)
def publish_site_view(request):
    try:
        body = {}
        try:
            body = request.json_body
        except Exception:
            pass
        slug = body.get("slug") or request.params.get("slug")
        if not slug:
            return Response(body=json.dumps({"error": "Missing slug"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        meta_root = os.path.join(data_dir, "publish_meta")
        # A public lookup by slug, with no owner known in advance -- has to walk both levels of
        # publish_meta/{owner}/{vault_id}/.
        if os.path.isdir(meta_root):
            for owner in os.listdir(meta_root):
                owner_meta_dir = os.path.join(meta_root, owner)
                if not os.path.isdir(owner_meta_dir):
                    continue
                for vault_id in os.listdir(owner_meta_dir):
                    vault_meta_dir = os.path.join(owner_meta_dir, vault_id)
                    if not os.path.isdir(vault_meta_dir):
                        continue
                    site = load_publish_meta(data_dir, owner, vault_id, "site.json", {})
                    if site.get("slug") == slug:
                        return {"id": vault_id, "slug": slug, "host": site.get("host", "")}

        return Response(body=json.dumps({"error": "Not found"}).encode("utf-8"), status=404, content_type="application/json")
    except Exception as e:
        logger.error(f"HTTP publish_site failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_customurl', renderer='json', permission=NO_PERMISSION_REQUIRED)
def publish_customurl_view(request):
    return {"url": "", "redirect": False}

@view_config(route_name='publish_slug', renderer='json', permission='vault-access')
def publish_slug_view(request):
    try:
        body = request.json_body
        vault_id = request.context.vault_id
        host = body.get("host", "")
        slug = body.get("slug", "")

        data_dir = request.registry.settings.get("data_dir")
        owner = request.context.owner
        site = load_publish_meta(data_dir, owner, vault_id, "site.json", {})
        site["slug"] = slug
        site["host"] = host
        save_publish_meta(data_dir, owner, vault_id, "site.json", site)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_slug failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_password', renderer='json', permission='vault-access')
def publish_password_view(request):
    try:
        body = request.json_body
        vault_id = request.context.vault_id
        owner = request.context.owner

        data_dir = request.registry.settings.get("data_dir")
        passwords = load_publish_meta(data_dir, owner, vault_id, "passwords.json", [])

        name = body.get("name")
        pw = body.get("pw")
        del_name = body.get("del")

        if del_name:
            passwords = [p for p in passwords if p.get("name") != del_name]
            save_publish_meta(data_dir, owner, vault_id, "passwords.json", passwords)
            return {"ok": True}
        elif name and pw:
            passwords = [p for p in passwords if p.get("name") != name]
            passwords.append({"name": name, "pw": pw})
            save_publish_meta(data_dir, owner, vault_id, "passwords.json", passwords)
            return {"ok": True}
        else:
            # GET: return list without pw values
            return {"pass": [{"name": p["name"]} for p in passwords]}
    except Exception as e:
        logger.error(f"HTTP publish_password failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")


@view_config(route_name='publish_share_list', renderer='json', permission='vault-access')
def publish_share_list_view(request):
    try:
        vault_id = request.context.vault_id
        data_dir = request.registry.settings.get("data_dir")
        shares = load_publish_meta(data_dir, request.context.owner, vault_id, "shares.json", [])
        # Don't expose invite_code externally
        public_shares = [
            {"uid": s["uid"], "email": s["email"], "name": s.get("name", ""), "accepted": s.get("accepted", False)}
            for s in shares
        ]
        return {"shares": public_shares}
    except Exception as e:
        logger.error(f"HTTP publish_share_list failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_invite', renderer='json', permission='vault-access')
def publish_share_invite_view(request):
    try:
        body = request.json_body
        vault_id = request.context.vault_id
        email = body.get("email")
        if not email:
            return Response(body=json.dumps({"error": "Missing site_uid or email"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        owner = request.context.owner
        shares = load_publish_meta(data_dir, owner, vault_id, "shares.json", [])
        new_share = {
            "uid": str(uuid.uuid4()),
            "email": email,
            "name": "",
            "accepted": False,
            "invite_code": str(uuid.uuid4()),
        }
        shares.append(new_share)
        save_publish_meta(data_dir, owner, vault_id, "shares.json", shares)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_share_invite failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_remove', renderer='json', permission='vault-access')
def publish_share_remove_view(request):
    try:
        body = request.json_body
        vault_id = request.context.vault_id
        share_uid = body.get("share_uid")
        if not share_uid:
            return Response(body=json.dumps({"error": "Missing site_uid or share_uid"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        owner = request.context.owner
        shares = load_publish_meta(data_dir, owner, vault_id, "shares.json", [])
        shares = [s for s in shares if s.get("uid") != share_uid]
        save_publish_meta(data_dir, owner, vault_id, "shares.json", shares)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_share_remove failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_accept', renderer='json', permission=NO_PERMISSION_REQUIRED)
def publish_share_accept_view(request):
    try:
        body = request.json_body
        code = body.get("code")
        if not code:
            return Response(body=json.dumps({"error": "Missing code"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        meta_root = os.path.join(data_dir, "publish_meta")
        # A public lookup by invite code, with no owner known in advance -- has to walk both
        # levels of publish_meta/{owner}/{vault_id}/.
        if os.path.isdir(meta_root):
            for owner in os.listdir(meta_root):
                owner_meta_dir = os.path.join(meta_root, owner)
                if not os.path.isdir(owner_meta_dir):
                    continue
                for vault_id in os.listdir(owner_meta_dir):
                    vault_meta_dir = os.path.join(owner_meta_dir, vault_id)
                    if not os.path.isdir(vault_meta_dir):
                        continue
                    shares = load_publish_meta(data_dir, owner, vault_id, "shares.json", [])
                    changed = False
                    for s in shares:
                        if s.get("invite_code") == code:
                            s["accepted"] = True
                            changed = True
                    if changed:
                        save_publish_meta(data_dir, owner, vault_id, "shares.json", shares)
                        return {"ok": True}

        return Response(body=json.dumps({"error": "Invalid code"}).encode("utf-8"), status=404, content_type="application/json")
    except Exception as e:
        logger.error(f"HTTP publish_share_accept failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")


# ─── Vault sync sharing (see pumice's 14_vault_sharing_설계.md) ────────────────────────────────
# Distinct from the /publish/share/* views above: those grant read access to a published static
# site; these grant full read/write sync access to the vault itself, backed by the vault_shares
# DB table (repository.py) rather than the JSON-file-per-vault pattern publish sharing uses --
# sync access is checked on every WS connect (SyncWsSession._handle_init), not just an occasional
# REST call, so an indexed table is the right fit here.

@view_config(route_name='vault_share_invite', renderer='json', permission='vault-access')
def vault_share_invite_view(request):
    try:
        body = request.json_body
        email = (body.get("email") or "").strip()
        if not email:
            return Response(body=json.dumps({"error": "Missing email"}).encode("utf-8"), status=400, content_type="application/json")

        share_uid = request.repository.add_vault_share(request.context.owner, request.context.vault_id, email)
        return {"ok": True, "uid": share_uid}
    except Exception as e:
        logger.error(f"HTTP vault_share_invite failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='vault_share_list', renderer='json', permission='vault-access')
def vault_share_list_view(request):
    try:
        shares = request.repository.list_vault_shares(request.context.owner, request.context.vault_id)
        public_shares = [
            {"uid": s["uid"], "email": s["email"], "accepted": s["accepted"], "createdAtMs": s["created_at_ms"]}
            for s in shares
        ]
        return {"shares": public_shares}
    except Exception as e:
        logger.error(f"HTTP vault_share_list failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='vault_share_remove', renderer='json', permission='vault-access')
def vault_share_remove_view(request):
    try:
        body = request.json_body
        share_uid = body.get("share_uid")
        if not share_uid:
            return Response(body=json.dumps({"error": "Missing share_uid"}).encode("utf-8"), status=400, content_type="application/json")

        ok = request.repository.remove_vault_share(request.context.owner, request.context.vault_id, share_uid)
        return {"ok": ok}
    except Exception as e:
        logger.error(f"HTTP vault_share_remove failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='vault_share_leave', renderer='json', permission='authenticated')
def vault_share_leave_view(request):
    # Not VaultContext-scoped: the caller here is the invited collaborator, not the vault's
    # owner, so VaultContext.owner (always the caller's own identity) would resolve to the
    # wrong account -- ownerUsername/vaultId are read directly from the body instead.
    try:
        body = request.json_body
        owner_username = body.get("ownerUsername")
        vault_id = body.get("vaultId")
        if not owner_username or not vault_id:
            return Response(body=json.dumps({"error": "Missing ownerUsername or vaultId"}).encode("utf-8"), status=400, content_type="application/json")

        caller = request.identity["username"]
        ok = request.repository.remove_vault_share_by_invited_user(caller, owner_username, vault_id)
        return {"ok": ok}
    except Exception as e:
        logger.error(f"HTTP vault_share_leave failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='vault_shared_with_me', renderer='json', permission='authenticated')
def vault_shared_with_me_view(request):
    try:
        caller = request.identity["username"]
        shared = request.repository.list_shared_vaults_for_user(caller)
        vaults = [
            {"ownerUsername": s["owner_username"], "vaultId": s["vault_id"], "accepted": s["accepted"], "createdAtMs": s["created_at_ms"]}
            for s in shared
        ]
        return {"vaults": vaults}
    except Exception as e:
        logger.error(f"HTTP vault_shared_with_me failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")


@view_config(route_name='user_logout', renderer='json', permission=NO_PERMISSION_REQUIRED)
def user_logout_view(request):
    token = extract_token(request)
    if token:
        request.repository.delete_device_token(token)
        logger.info("Session logged out.")
    clear_session_cookie(request)
    return {"ok": True}

@view_config(route_name='admin_users', renderer='json', permission='admin')
def admin_users_view(request):
    try:
        page = int(request.params.get("page", 1))
        limit = int(request.params.get("limit", 10))
    except ValueError:
        page = 1
        limit = 10

    if page < 1:
        page = 1
    if limit < 1:
        limit = 10

    users = request.repository.get_all_users()
    total = len(users)

    start = (page - 1) * limit
    end = start + limit
    sliced_users = users[start:end]

    return {
        "users": sliced_users,
        "total": total,
        "page": page,
        "limit": limit
    }

@view_config(route_name='admin_user_delete', renderer='json', permission='admin')
def admin_user_delete_view(request):
    user_info = request.identity
    try:
        body = request.json_body
        username_to_delete = body.get("username")
        if not username_to_delete:
            return Response(status=400, body=json.dumps({"error": "Missing username"}).encode("utf-8"), content_type="application/json")

        if username_to_delete == user_info["username"]:
            return Response(status=400, body=json.dumps({"error": "Cannot delete currently logged in admin user"}).encode("utf-8"), content_type="application/json")
            
        request.repository.delete_user(username_to_delete)
        logger.info(f"Admin '{user_info['username']}' deleted user '{username_to_delete}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to delete user: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='admin_user_reset_password', renderer='json', permission='admin')
def admin_user_reset_password_view(request):
    user_info = request.identity
    try:
        body = request.json_body
        username = body.get("username")
        new_password = body.get("password")
        if not username or not new_password:
            return Response(status=400, body=json.dumps({"error": "Missing username or password"}).encode("utf-8"), content_type="application/json")

        pw_hash = hash_password(new_password)
        request.repository.update_user_password(username, pw_hash)
        logger.info(f"Admin '{user_info['username']}' changed password for user '{username}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to reset user password: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='admin_user_create', renderer='json', permission='admin')
def admin_user_create_view(request):
    user_info = request.identity
    try:
        body = request.json_body
        username = body.get("username")
        password = body.get("password")
        name = body.get("name")
        email = body.get("email")
        if not username or not password:
            return Response(status=400, body=json.dumps({"error": "Missing username or password"}).encode("utf-8"), content_type="application/json")
            
        pw_hash = hash_password(password)
        success = request.repository.create_user(
            username=username,
            password_hash=pw_hash,
            name=name,
            email=email,
            is_admin=False
        )
        if not success:
            return Response(status=400, body=json.dumps({"error": "User already exists"}).encode("utf-8"), content_type="application/json")
        logger.info(f"Admin '{user_info['username']}' created user '{username}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to create user: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

# ─── Server-side publish plugins (skeleton) ─────────────────────────────────
# Discovery logic lives in plugin_registry.py (no Twisted/Pyramid dependency, see its own
# module docstring). There's no admin "install" action here at all -- a plugin is baked into
# the server's Docker image at build time, as a regular pumice-server dependency, so whatever
# discover_plugins() finds is simply whatever the running image was built with. *Enabling* a
# discovered plugin is the only runtime action, and it's self-service: each logged-in account
# turns plugins on/off for itself (see the user_plugin_* views further down,
# permission='authenticated'), same as any other per-account setting on this server. No plugin
# actually runs anything yet -- there's no publish-renderer integration in this pass, just
# discovery + enable/disable bookkeeping.

@view_config(route_name='admin_plugins', renderer='json', permission='admin')
def admin_plugins_view(request):
    manifests = discover_plugins()
    return {
        "installed": [
            {
                "id": m.plugin_id,
                "name": m.plugin_id,
                "version": m.version,
                "description": m.description,
            }
            for m in manifests
        ],
        "enabled": request.repository.list_all_enabled_plugins(),
    }

def _plugin_config_entries_for_display(request, owner, vault_id, plugin_id, config_fields):
    # Password-type fields are never sent back to the browser once saved -- the settings form
    # shows them blank (see publish_plugin_config_save_view's merge-on-blank rule), so a
    # decrypted password is never round-tripped back out over the wire after the moment it was
    # first entered.
    if not config_fields:
        return {}
    password_keys = {f["key"] for f in config_fields if f.get("type") == "password"}
    raw_entries = request.repository.get_plugin_config_entries(owner, vault_id, plugin_id)
    result = {}
    for entry_id, row in raw_entries.items():
        try:
            values = decrypt_json(row["config_blob_b64"])
        except PluginConfigEncryptionError as e:
            logger.error(f"Failed to decrypt plugin config entry '{entry_id}' for plugin '{plugin_id}': {e}")
            continue
        result[entry_id] = {k: v for k, v in values.items() if k not in password_keys}
    return result

@view_config(route_name='publish_plugins', renderer='json', permission='vault-access')
def publish_plugins_view(request):
    # Vault-scoped, not account-scoped -- lives in the publish tab next to the vault's other
    # publish settings (slug, password, ...), not in My Page, because "should this plugin's
    # syntax get resolved when THIS vault is published" is a per-vault choice, the same way
    # installing/enabling a plugin in Obsidian itself is scoped per vault, not per account.
    vault_id = request.context.vault_id
    owner = request.context.owner
    manifests = discover_plugins()
    enabled_ids = {p["plugin_id"] for p in request.repository.list_enabled_plugins(owner, vault_id)}
    installed = []
    for m in manifests:
        config_fields = load_plugin_config_fields(m.plugin_id)
        installed.append({
            "id": m.plugin_id,
            "name": m.plugin_id,
            "version": m.version,
            "description": m.description,
            "enabled": m.plugin_id in enabled_ids,
            "config_fields": config_fields,
            "config_entries": _plugin_config_entries_for_display(request, owner, vault_id, m.plugin_id, config_fields),
        })
    return {"installed": installed}

@view_config(route_name='publish_plugin_enable', renderer='json', permission='vault-access')
def publish_plugin_enable_view(request):
    vault_id = request.context.vault_id
    owner = request.context.owner
    try:
        body = request.json_body
        plugin_id = body.get("plugin_id")
        if not plugin_id:
            return Response(status=400, body=json.dumps({"error": "Missing plugin_id"}).encode("utf-8"), content_type="application/json")

        manifests = discover_plugins()
        if plugin_id not in {m.plugin_id for m in manifests}:
            return Response(status=400, body=json.dumps({"error": f"Plugin '{plugin_id}' is not installed"}).encode("utf-8"), content_type="application/json")

        request.repository.enable_plugin(owner, vault_id, plugin_id, int(time.time() * 1000))
        logger.info(f"User '{owner}' enabled plugin '{plugin_id}' for vault '{vault_id}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to enable plugin: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='publish_plugin_disable', renderer='json', permission='vault-access')
def publish_plugin_disable_view(request):
    vault_id = request.context.vault_id
    owner = request.context.owner
    try:
        body = request.json_body
        plugin_id = body.get("plugin_id")
        if not plugin_id:
            return Response(status=400, body=json.dumps({"error": "Missing plugin_id"}).encode("utf-8"), content_type="application/json")

        request.repository.disable_plugin(owner, vault_id, plugin_id)
        logger.info(f"User '{owner}' disabled plugin '{plugin_id}' for vault '{vault_id}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to disable plugin: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='publish_plugin_config_save', renderer='json', permission='vault-access')
def publish_plugin_config_save_view(request):
    vault_id = request.context.vault_id
    owner = request.context.owner
    try:
        body = request.json_body
        plugin_id = body.get("plugin_id")
        entry_id = (body.get("entry_id") or "").strip()
        values = body.get("values") or {}
        if not plugin_id or not entry_id:
            return Response(status=400, body=json.dumps({"error": "Missing plugin_id or entry_id"}).encode("utf-8"), content_type="application/json")

        config_fields = load_plugin_config_fields(plugin_id)
        if not config_fields:
            return Response(status=400, body=json.dumps({"error": f"Plugin '{plugin_id}' does not declare any config fields"}).encode("utf-8"), content_type="application/json")

        # A blank password field means "keep whatever's already stored" -- the form never shows
        # a saved password back to the browser (see _plugin_config_entries_for_display), so
        # there's no other value it could resubmit for a field the vault owner didn't touch.
        existing_raw = request.repository.get_plugin_config_entries(owner, vault_id, plugin_id).get(entry_id)
        existing_values = {}
        if existing_raw:
            try:
                existing_values = decrypt_json(existing_raw["config_blob_b64"])
            except PluginConfigEncryptionError:
                existing_values = {}

        merged = dict(existing_values)
        for field in config_fields:
            key = field["key"]
            if key not in values:
                continue
            new_value = values[key]
            if field.get("type") == "password" and not new_value:
                continue
            merged[key] = new_value

        blob = encrypt_json(merged)
        request.repository.set_plugin_config_entry(owner, vault_id, plugin_id, entry_id, blob, int(time.time() * 1000))
        logger.info(f"User '{owner}' saved config entry '{entry_id}' for plugin '{plugin_id}' on vault '{vault_id}'")
        return {"ok": True}
    except PluginConfigEncryptionError as e:
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")
    except Exception as e:
        logger.error(f"Failed to save plugin config: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='publish_plugin_config_delete', renderer='json', permission='vault-access')
def publish_plugin_config_delete_view(request):
    vault_id = request.context.vault_id
    owner = request.context.owner
    try:
        body = request.json_body
        plugin_id = body.get("plugin_id")
        entry_id = body.get("entry_id")
        if not plugin_id or not entry_id:
            return Response(status=400, body=json.dumps({"error": "Missing plugin_id or entry_id"}).encode("utf-8"), content_type="application/json")

        request.repository.delete_plugin_config_entry(owner, vault_id, plugin_id, entry_id)
        logger.info(f"User '{owner}' deleted config entry '{entry_id}' for plugin '{plugin_id}' on vault '{vault_id}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to delete plugin config entry: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='publish_plugin_autofill', renderer='json', permission='vault-access')
def publish_plugin_autofill_view(request):
    # Best-effort, always 200 with (possibly empty) suggestions -- autofill is a convenience on
    # top of manually filling in the settings form, never something the modal opening should be
    # blocked on. See plugin_registry.py's docstring and docs/PLUGIN_DEVELOPMENT.md for the
    # mechanism: a plugin declares which of its OWN vault-relative files it wants read (e.g.
    # another plugin's already-synced, non-secret data.json) and a function to turn their
    # contents into suggested config entries.
    vault_id = request.context.vault_id
    owner = request.context.owner
    try:
        body = request.json_body
        plugin_id = body.get("plugin_id")
        if not plugin_id:
            return Response(status=400, body=json.dumps({"error": "Missing plugin_id"}).encode("utf-8"), content_type="application/json")

        paths = load_plugin_autofill_paths(plugin_id)
        if not paths:
            return {"suggestions": []}

        data_dir = request.registry.settings.get("data_dir")
        vault_path = get_vault_path(data_dir, owner, vault_id)
        vault_files = read_vault_files(vault_path, paths)

        suggestions = suggest_plugin_autofill(plugin_id, vault_files)
        config_fields = load_plugin_config_fields(plugin_id) or []
        return {"suggestions": sanitize_autofill_suggestions(suggestions, config_fields)}
    except Exception as e:
        logger.error(f"Failed to compute plugin autofill suggestions: {e}")
        return {"suggestions": []}

@view_config(route_name='admin_user_devices', renderer='json', permission='admin')
def admin_user_devices_view(request):
    target_username = request.matchdict.get("username")
    devices = request.repository.list_device_tokens(target_username)
    return {"devices": devices}

@view_config(route_name='admin_user_device_delete', renderer='json', permission='admin')
def admin_user_device_delete_view(request):
    user_info = request.identity
    target_username = request.matchdict.get("username")
    try:
        body = request.json_body
        token = body.get("token")
        if not token:
            return Response(status=400, body=json.dumps({"error": "Missing token"}).encode("utf-8"), content_type="application/json")

        device = request.repository.get_device_token(token)
        if not device or device["username"] != target_username:
            return Response(status=404, body=json.dumps({"error": "Device token not found"}).encode("utf-8"), content_type="application/json")

        request.repository.delete_device_token(token)
        logger.info(f"Admin '{user_info['username']}' revoked a device token for user '{target_username}'")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to revoke device token: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='user_devices', renderer='json', permission='authenticated')
def user_devices_view(request):
    user_info = request.identity
    devices = request.repository.list_device_tokens(user_info["username"])
    return {"devices": devices}

@view_config(route_name='user_device_create', renderer='json', permission='authenticated')
def user_device_create_view(request):
    # Nextcloud-style "app password" flow: a device token minted from an already-logged-in
    # session, for pasting into a client that isn't going to do an interactive username+password
    # login itself (the Obsidian plugin, a script, ...) -- same device_tokens row/shape
    # user_login_view creates, just not gated behind re-entering the account password.
    user_info = request.identity
    try:
        body = request.json_body
        device_name = (body.get("device_name") or "").strip()
        if not device_name:
            return Response(status=400, body=json.dumps({"error": "Missing device_name"}).encode("utf-8"), content_type="application/json")

        session_token = secrets.token_hex(32)
        request.repository.create_device_token(session_token, user_info["username"], device_name, int(time.time() * 1000))
        logger.info(f"User '{user_info['username']}' created a new device token for '{device_name}'.")
        return {"ok": True, "device_token": session_token, "device_name": device_name}
    except Exception as e:
        logger.error(f"Failed to create device token: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='user_device_delete', renderer='json', permission='authenticated')
def user_device_delete_view(request):
    user_info = request.identity
    try:
        body = request.json_body
        token = body.get("token")
        if not token:
            return Response(status=400, body=json.dumps({"error": "Missing token"}).encode("utf-8"), content_type="application/json")

        device = request.repository.get_device_token(token)
        if not device or device["username"] != user_info["username"]:
            return Response(status=404, body=json.dumps({"error": "Device token not found"}).encode("utf-8"), content_type="application/json")

        request.repository.delete_device_token(token)
        logger.info(f"User '{user_info['username']}' revoked their own device token.")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to revoke own device token: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='user_profile', renderer='json', permission='authenticated')
def user_profile_view(request):
    username = request.identity["username"]

    # Look up the user in the DB (the env-var admin account has a real DB row too, created at
    # startup -- see main.py)
    user = request.repository.get_user_by_username(username)
    if not user:
        return Response(status=404, body=json.dumps({"error": "User not found"}).encode("utf-8"), content_type="application/json")
        
    return {
        "username": user["username"],
        "name": user.get("name") or "",
        "email": user.get("email") or "",
        "is_admin": bool(user.get("is_admin", False))
    }

@view_config(route_name='user_profile_update', renderer='json', permission='authenticated')
def user_profile_update_view(request):
    username = request.identity["username"]
    
    # Block the env-var admin from editing their profile directly
    admin_user = os.getenv("ADMIN_USER") or "admin"
    if username == admin_user:
        return Response(status=400, body=json.dumps({"error": "Cannot update default system administrator profiles from the UI."}).encode("utf-8"), content_type="application/json")
        
    try:
        body = request.json_body
        name = body.get("name")
        email = body.get("email")
        new_password = body.get("password")
        
        if not name or not email:
            return Response(status=400, body=json.dumps({"error": "Name and email are required"}).encode("utf-8"), content_type="application/json")
            
        # Update the profile (name, email)
        request.repository.update_user_profile(username, name, email)

        # If a password change was requested
        if new_password and new_password.strip():
            pw_hash = hash_password(new_password)
            request.repository.update_user_password(username, pw_hash)
            logger.info(f"User '{username}' updated their own password.")
            
        logger.info(f"User '{username}' updated their profile info (Name: {name}, Email: {email}).")
        return {"ok": True, "name": name, "email": email}
    except Exception as e:
        logger.error(f"Failed to update profile: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

def create_pyramid_app(repository, data_dir):
    settings = {
        "repository": repository,
        "data_dir": data_dir,
    }
    # Initialize the Pyramid Configurator
    config = Configurator(settings=settings)
    config.include('pyramid_jinja2')
    config.add_static_view(name='static', path='pumice_server:static')

    # Register a helper attribute so views can reach the DB repository via request.repository
    config.add_request_method(lambda r: r.registry.settings["repository"], "repository", reify=True)

    # Add tweens: a tween added later runs on the outermost layer
    config.add_tween("pumice_server.web.cors_tween_factory")

    # Authorization (see the "Authorization (Pyramid ACL)" section above): every route requires
    # at least a valid identity unless its view explicitly opts out with
    # permission=NO_PERMISSION_REQUIRED -- secure by default, instead of the old tween's
    # manually-maintained bypass list (easy to forget to update, and it silently defaulted to
    # *open* rather than *closed*).
    config.set_security_policy(DeviceTokenSecurityPolicy())
    config.set_root_factory(RootFactory)
    config.set_default_permission("authenticated")

    # Add routes. Endpoints scoped to a single vault get VaultContext as their route factory,
    # which resolves vault_id (from matchdict/params/obs-id header/JSON body, whichever that
    # endpoint uses) and builds its ACL from ownership -- paired with permission='vault-access'
    # on the view itself.
    config.add_route('root', '/')
    config.add_route('ping', '/api/ping')
    config.add_route('login_page', '/login')
    config.add_route('dashboard_page', '/dashboard')
    config.add_route('user_login', '/user/login')
    config.add_route('user_logout', '/user/logout')
    config.add_route('user_profile', '/api/user/profile')
    config.add_route('user_profile_update', '/api/user/profile/update')
    config.add_route('user_devices', '/api/user/devices')
    config.add_route('user_device_create', '/api/user/devices/create')
    config.add_route('user_device_delete', '/api/user/devices/delete')
    # Vault sync sharing (see pumice's 14_vault_sharing_설계.md) -- distinct from the
    # /publish/share/* routes above, which grant read access to a published static site, not
    # to a vault's own sync data. invite/list/remove are owner-only (VaultContext); leave/
    # shared-with-me act on the caller's own account instead, so they use the default
    # RootFactory (permission='authenticated' is all they need -- see RootFactory.__acl__).
    config.add_route('vault_share_invite', '/api/vault/share/invite', factory=VaultContext)
    config.add_route('vault_share_list', '/api/vault/share/list', factory=VaultContext)
    config.add_route('vault_share_remove', '/api/vault/share/remove', factory=VaultContext)
    config.add_route('vault_share_leave', '/api/vault/share/leave')
    config.add_route('vault_shared_with_me', '/api/vault/shared-with-me')
    config.add_route('admin_users', '/api/admin/users')
    config.add_route('admin_user_create', '/api/admin/users/create')
    config.add_route('admin_user_delete', '/api/admin/users/delete')
    config.add_route('admin_user_reset_password', '/api/admin/users/reset-password')
    config.add_route('admin_user_devices', '/api/admin/users/{username}/devices')
    config.add_route('admin_user_device_delete', '/api/admin/users/{username}/devices/delete')
    config.add_route('admin_vaults', '/api/admin/vaults')
    config.add_route('admin_vault_files', '/api/admin/vaults/{vault_id}', factory=VaultContext)
    config.add_route('admin_published', '/api/admin/published')
    config.add_route('admin_plugins', '/api/admin/plugins')
    config.add_route('get_token_info', '/api/token/info')
    config.add_route('oauth_protected_resource_metadata', '/.well-known/oauth-protected-resource')
    config.add_route('oauth_authorization_server_metadata', '/.well-known/oauth-authorization-server')
    config.add_route('oauth_register', '/oauth/register')
    config.add_route('oauth_authorize', '/oauth/authorize')
    config.add_route('oauth_token', '/oauth/token')
    config.add_route('publish_list', '/api/list', factory=VaultContext)
    config.add_route('publish_upload', '/api/upload', factory=VaultContext)
    config.add_route('publish_remove', '/api/remove', factory=VaultContext)
    config.add_route('publish_download', '/api/download', factory=VaultContext)
    config.add_route('publish_slugs', '/api/slugs')
    config.add_route('publish_site', '/api/site')
    config.add_route('publish_customurl', '/api/customurl')
    config.add_route('publish_slug', '/api/slug', factory=VaultContext)
    config.add_route('publish_password', '/api/password', factory=VaultContext)
    config.add_route('publish_plugins', '/api/publish/plugins', factory=VaultContext)
    config.add_route('publish_plugin_enable', '/api/publish/plugins/enable', factory=VaultContext)
    config.add_route('publish_plugin_disable', '/api/publish/plugins/disable', factory=VaultContext)
    config.add_route('publish_plugin_config_save', '/api/publish/plugins/config/save', factory=VaultContext)
    config.add_route('publish_plugin_config_delete', '/api/publish/plugins/config/delete', factory=VaultContext)
    config.add_route('publish_plugin_autofill', '/api/publish/plugins/autofill', factory=VaultContext)
    # Register the specific /publish/share/* routes before the wildcard /publish/{vault_id}/{path:.*}
    config.add_route('publish_share_list', '/publish/share/list', factory=VaultContext)
    config.add_route('publish_share_invite', '/publish/share/invite', factory=VaultContext)
    config.add_route('publish_share_remove', '/publish/share/remove', factory=VaultContext)
    config.add_route('publish_share_accept', '/publish/share/accept')
    config.add_route('publish_view', '/publish/{username}/{vault_id}/{path:.*}')

    # Scan for decorators (@view_config, etc.)
    config.scan(__name__)
    
    logger.info("Pyramid Web Application successfully created and configured.")
    return config.make_wsgi_app()
