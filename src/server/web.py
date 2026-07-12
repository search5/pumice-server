import time
import json
import logging
import re
import os
import shutil
import uuid
import i18n
from urllib.parse import quote, unquote
from pyramid.config import Configurator
from pyramid.response import Response
from pyramid.view import view_config

logger = logging.getLogger("server.web")

# ─── i18n ────────────────────────────────────────────────────────────────
# Locale is negotiated per-request from the browser's Accept-Language header --
# no client-side language detection needed. Translation text lives entirely in
# locale/{lang}.json (not in this file); i18n.t() is called with an explicit
# `locale=` on every lookup rather than i18n.set('locale', ...), since that's
# global mutable state and this server handles requests concurrently (Twisted
# + asyncio) -- a per-call locale avoids any cross-request race.
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
i18n.load_path.append(_LOCALE_DIR)
i18n.set("file_format", "json")
i18n.set("filename_format", "{locale}.{format}")
i18n.set("fallback", "ko")

_SUPPORTED_LOCALES = ["ko", "en"]
_I18N_PARAM_RE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")

# Raw locale dicts, loaded once and cached, for embedding a fully-resolved
# translation blob into a page's <script> for the handful of strings that JS
# generates at runtime (toasts, confirm dialogs, fetch-rendered table rows) --
# separate from i18n.t()'s own internal loading, so this doesn't depend on that
# library's private cache structure.
_LOCALE_DATA_CACHE: dict = {}

def get_locale_dict(lang: str) -> dict:
    if lang not in _LOCALE_DATA_CACHE:
        with open(os.path.join(_LOCALE_DIR, f"{lang}.json"), "r", encoding="utf-8") as f:
            _LOCALE_DATA_CACHE[lang] = json.load(f)[lang]
    return _LOCALE_DATA_CACHE[lang]

def detect_lang(request) -> str:
    return request.accept_language.best_match(_SUPPORTED_LOCALES, default_match="ko")

def t(key: str, lang: str, **params) -> str:
    text = i18n.t(key, locale=lang)
    if params:
        text = _I18N_PARAM_RE.sub(lambda m: str(params.get(m.group(1), "")), text)
    return text

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

@view_config(route_name='login_page', permission=NO_PERMISSION_REQUIRED)
def login_page_view(request):
    lang = detect_lang(request)
    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="' + lang + '">\n<head>\n')
    parts.append("""    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + t('login.page_title', lang) + """</title>""")
    parts.append("""
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --md-sys-color-primary: #a8c7fa; /* Google-blue family */
            --md-sys-color-on-primary: #062e6f;
            --md-sys-color-primary-container: #004a77;
            --md-sys-color-on-primary-container: #c2e7ff;
            --md-sys-color-surface: #121318;
            --md-sys-color-surface-container: #1f2025;
            --md-sys-color-on-surface: #e2e2e9;
            --md-sys-color-on-surface-variant: #c4c6d0;
            --md-sys-color-outline: #8e9099;
            --md-sys-color-error: #ffb4ab;
            --md-sys-color-on-error: #690005;
        }
        
        body.light-theme {
            --md-sys-color-primary: #3f5f90;
            --md-sys-color-on-primary: #ffffff;
            --md-sys-color-primary-container: #d6e3ff;
            --md-sys-color-on-primary-container: #001b3d;
            --md-sys-color-surface: #f9f9fc;
            --md-sys-color-surface-container: #f0f0f4;
            --md-sys-color-on-surface: #191c20;
            --md-sys-color-on-surface-variant: #43474e;
            --md-sys-color-outline: #73777f;
            --md-sys-color-error: #ba1a1a;
            --md-sys-color-on-error: #ffffff;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--md-sys-color-surface);
            color: var(--md-sys-color-on-surface);
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            position: relative;
            transition: background-color 0.3s ease;
        }
        
        .theme-fab {
            position: absolute;
            top: 24px;
            right: 24px;
            background-color: var(--md-sys-color-surface-container);
            border: 1px solid var(--md-sys-color-outline);
            color: var(--md-sys-color-on-surface);
            padding: 10px 20px;
            border-radius: 100px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }
        .theme-fab:hover {
            opacity: 0.9;
            border-color: var(--md-sys-color-primary);
        }

        .login-card {
            background-color: var(--md-sys-color-surface-container);
            border-radius: 28px; /* M3 Card style */
            padding: 40px;
            width: 100%;
            max-width: 440px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.05);
            transition: transform 0.2s ease, background-color 0.3s ease;
        }
        .login-card:hover {
            transform: translateY(-2px);
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
        }
        .logo {
            font-family: 'Outfit', sans-serif;
            font-size: 32px;
            font-weight: 700;
            color: var(--md-sys-color-primary);
            letter-spacing: -0.5px;
            margin-bottom: 6px;
        }
        .subtitle {
            font-size: 14px;
            color: var(--md-sys-color-on-surface-variant);
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group.hidden {
            display: none;
        }
        .form-group label {
            display: block;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 8px;
            color: var(--md-sys-color-on-surface);
            padding-left: 4px;
        }
        .form-group input {
            width: 100%;
            background-color: transparent;
            border: 1px solid var(--md-sys-color-outline);
            border-radius: 14px; /* M3 Outlined text field style */
            padding: 14px 16px;
            color: var(--md-sys-color-on-surface);
            font-family: inherit;
            font-size: 15px;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }
        .form-group input:focus {
            outline: none;
            border-color: var(--md-sys-color-primary);
            border-width: 2px;
            box-shadow: 0 0 0 3px rgba(168, 199, 250, 0.15);
        }
        .btn {
            width: 100%;
            background-color: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
            border: none;
            border-radius: 100px; /* M3 Pill button style */
            padding: 14px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s ease, transform 0.1s ease;
        }
        .btn:hover {
            opacity: 0.92;
        }
        .btn:active {
            transform: scale(0.98);
        }
        .alert {
            padding: 12px 16px;
            border-radius: 12px;
            font-size: 13px;
            margin-bottom: 20px;
            display: none;
        }
        .alert-error {
            background-color: rgba(255, 180, 171, 0.15);
            border: 1px solid var(--md-sys-color-error);
            color: var(--md-sys-color-error);
        }
        .alert-success {
            background-color: rgba(168, 199, 250, 0.15);
            border: 1px solid var(--md-sys-color-primary);
            color: var(--md-sys-color-primary);
        }
        .result-box {
            margin-top: 25px;
            background-color: rgba(0, 0, 0, 0.1);
            border: 1px solid var(--md-sys-color-outline);
            border-radius: 16px;
            padding: 20px;
            display: none;
            word-break: break-all;
        }
        .result-title {
            font-size: 14px;
            font-weight: 600;
            color: var(--md-sys-color-primary);
            margin-bottom: 8px;
        }
        .token-display {
            font-family: 'Fira Code', monospace;
            background-color: rgba(255, 255, 255, 0.05);
            padding: 12px;
            border-radius: 12px;
            font-size: 13px;
            user-select: all;
            margin-bottom: 12px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: var(--md-sys-color-on-surface);
        }
        .copy-hint {
            font-size: 12px;
            color: var(--md-sys-color-on-surface-variant);
            text-align: center;
        }
    </style>
</head>
<body>
    <button class="theme-fab" id="themeToggleBtn">
        <span id="themeToggleIcon">☀️</span> <span id="themeToggleText">""")
    parts.append(t('common.theme_light', lang))
    parts.append("""</span>
    </button>

    <div class="login-card">
        <div class="header">
            <div class="logo">Obsidian Sync</div>
            <div class="subtitle" id="cardSubtitle">""")
    parts.append(t('login.subtitle', lang))
    parts.append("""</div>
        </div>
        <div id="alert" class="alert alert-error"></div>
        <form id="loginForm">
            <div class="form-group">
                <label for="username">""")
    parts.append(t('login.label_username', lang))
    parts.append('</label>\n                <input type="text" id="username" required placeholder="')
    parts.append(t('login.placeholder_username', lang))
    parts.append('">\n            </div>\n            <div class="form-group">\n                <label for="password">')
    parts.append(t('login.label_password', lang))
    parts.append("""</label>
                <input type="password" id="password" required placeholder="••••••••">
            </div>

            <button type="submit" class="btn" id="submitBtn">""")
    parts.append(t('login.button_submit', lang))
    parts.append("""</button>
        </form>

        <div style="text-align: center; margin-top: 20px; font-size: 13px; color: var(--md-sys-color-on-surface-variant);">
            """)
    parts.append(t('login.no_account', lang))
    parts.append("""
        </div>

        <div id="resultBox" class="result-box">
            <div class="result-title">""")
    parts.append(t('login.result_title', lang))
    parts.append("""</div>
            <p style="font-size: 12px; margin-bottom: 12px; color: var(--md-sys-color-on-surface-variant);">""")
    parts.append(t('login.result_desc', lang))
    parts.append("""</p>
            <div class="token-display" id="tokenDisplay"></div>
            <div class="copy-hint">""")
    parts.append(t('login.result_redirecting', lang))
    parts.append("""</div>
        </div>

        <!-- 로그인이 obsidian://<action> 콜백으로 끝나는 경우 전용. 브라우저(특히 모바일)는 실제
             탭/클릭 없이 스크립트가 커스텀 스킴으로 이동시키는 걸 아무 표시 없이 막는 경우가 많아서,
             자동 리다이렉트 대신 사용자가 직접 눌러야 하는 링크로 보여준다. -->
        <div id="deviceReturnBox" class="result-box">
            <div class="result-title">""")
    parts.append(t('login.device_return_title', lang))
    parts.append("""</div>
            <p style="font-size: 12px; margin-bottom: 16px; color: var(--md-sys-color-on-surface-variant);">""")
    parts.append(t('login.device_return_desc', lang))
    parts.append("""</p>
            <a id="deviceReturnLink" class="btn" style="display: inline-block; text-decoration: none; text-align: center;" href="#">""")
    parts.append(t('login.device_return_button', lang))
    parts.append("""</a>
        </div>
    </div>

    <script>
        const I18N = """ + json.dumps(get_locale_dict(lang), ensure_ascii=False) + """;
        function t(key, params) {
            let node = I18N;
            for (const part of key.split(".")) {
                if (node == null) break;
                node = node[part];
            }
            let text = (typeof node === "string") ? node : key;
            if (params) {
                text = text.replace(/\\{\\{\\s*([\\w-]+)\\s*\\}\\}/g, (_m, name) => (params[name] !== undefined ? String(params[name]) : ""));
            }
            return text;
        }

        const form = document.getElementById("loginForm");
        const alertEl = document.getElementById("alert");
        const resultBox = document.getElementById("resultBox");
        const tokenDisplay = document.getElementById("tokenDisplay");
        const submitBtn = document.getElementById("submitBtn");
        const cardSubtitle = document.getElementById("cardSubtitle");
        const deviceReturnBox = document.getElementById("deviceReturnBox");
        const deviceReturnLink = document.getElementById("deviceReturnLink");

        // 테마 토글 기능 구현
        const themeToggleBtn = document.getElementById("themeToggleBtn");
        const themeToggleIcon = document.getElementById("themeToggleIcon");
        const themeToggleText = document.getElementById("themeToggleText");
        
        function applyTheme(theme) {
            if (theme === "light") {
                document.body.classList.add("light-theme");
                themeToggleIcon.innerText = "🌙";
                themeToggleText.innerText = t("common.theme_dark");
            } else {
                document.body.classList.remove("light-theme");
                themeToggleIcon.innerText = "☀️";
                themeToggleText.innerText = t("common.theme_light");
            }
        }

        // 브라우저 저장 테마 로드
        const savedTheme = localStorage.getItem("theme") || "dark";
        applyTheme(savedTheme);
        
        themeToggleBtn.addEventListener("click", () => {
            const currentTheme = document.body.classList.contains("light-theme") ? "light" : "dark";
            const nextTheme = currentTheme === "light" ? "dark" : "light";
            localStorage.setItem("theme", nextTheme);
            applyTheme(nextTheme);
        });

        // A client plugin (e.g. the Obsidian "Log in" button) opens this page with
        // ?redirect=obsidian://pumice-auth&device_name=... -- on success we hand back a
        // device_token via that custom URI instead of going to /dashboard. A device-pairing
        // visit always wants its own fresh token, so it never takes the already-logged-in
        // shortcut below even if this browser also has a valid session cookie.
        const params = new URLSearchParams(window.location.search);
        const deviceRedirect = params.get("redirect");
        const deviceName = params.get("device_name");

        if (deviceRedirect) {
            cardSubtitle.innerText = t("login.subtitle_device");
        }

        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            alertEl.style.display = "none";
            resultBox.style.display = "none";
            submitBtn.disabled = true;
            submitBtn.innerText = t("login.button_submitting");

            const username = document.getElementById("username").value;
            const password = document.getElementById("password").value;

            try {
                const payload = { email: username, password: password };
                if (deviceName) payload.device_name = deviceName;

                const response = await fetch("/user/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                const data = await response.json();
                if (response.ok) {
                    if (deviceRedirect) {
                        const back = new URL(deviceRedirect);
                        back.searchParams.set("token", data.device_token);
                        back.searchParams.set("username", data.username || data.email);

                        // Browsers (mobile ones especially) will often silently drop a
                        // script-triggered navigation to a custom scheme like obsidian:// once
                        // there's no fresh user gesture backing it (the click that submitted this
                        // form doesn't count anymore after the await above) -- no prompt, no
                        // error, it just does nothing. So instead of redirecting automatically,
                        // show a link the user taps themselves, which is a real gesture.
                        form.style.display = "none";
                        deviceReturnLink.href = back.toString();
                        deviceReturnBox.style.display = "block";
                        return;
                    }

                    alertEl.className = "alert alert-success";
                    alertEl.innerText = t("login.msg_success");
                    alertEl.style.display = "block";

                    // The actual credential is an HttpOnly session cookie the server just set on
                    // this response -- not readable (or needed) here. Only non-secret display
                    // state goes into localStorage.
                    localStorage.setItem("sync_username", data.username || data.email);
                    localStorage.setItem("sync_is_admin", data.is_admin);

                    window.location.href = "/dashboard";
                } else {
                    alertEl.className = "alert alert-error";
                    alertEl.innerText = data.error || t("login.msg_fail");
                    alertEl.style.display = "block";
                }
            } catch (err) {
                alertEl.className = "alert alert-error";
                alertEl.innerText = t("login.msg_network_error");
                alertEl.style.display = "block";
            } finally {
                submitBtn.disabled = false;
                submitBtn.innerText = t("login.button_submit");
            }
        });
    </script>
</body>
</html>""")
    html = "".join(parts)
    return Response(html, content_type="text/html")

@view_config(route_name='dashboard_page', permission=NO_PERMISSION_REQUIRED)
def dashboard_page_view(request):
    lang = detect_lang(request)
    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="' + lang + '">\n<head>\n')
    parts.append("""    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>""" + t('dashboard.page_title', lang) + """</title>""")
    parts.append("""
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            /* M3 Dark Theme Palette */
            --md-sys-color-primary: #d0bcff;
            --md-sys-color-on-primary: #381e72;
            --md-sys-color-primary-container: #4f378b;
            --md-sys-color-on-primary-container: #eaddff;
            --md-sys-color-surface: #141218;
            --md-sys-color-surface-container: #1d1b20;
            --md-sys-color-surface-container-high: #2b2930;
            --md-sys-color-on-surface: #e6e1e5;
            --md-sys-color-on-surface-variant: #cac4d0;
            --md-sys-color-outline: #938f99;
            --md-sys-color-error: #ffb4ab;
            --md-sys-color-on-error: #690005;
            
            --bg-color: var(--md-sys-color-surface);
            --sidebar-bg: var(--md-sys-color-surface-container);
            --card-bg: var(--md-sys-color-surface-container-high);
            --border-color: rgba(147, 143, 153, 0.2);
            --text-primary: var(--md-sys-color-on-surface);
            --text-muted: var(--md-sys-color-on-surface-variant);
            --accent-purple: var(--md-sys-color-primary);
            --accent-blue: #a8c7fa;
            --accent-green: #c2e7ff;
            --error-red: var(--md-sys-color-error);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg-color);
            color: var(--text-primary);
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            display: flex;
        }
        
        /* Sidebar */
        .sidebar {
            width: 280px;
            background: var(--sidebar-bg);
            display: flex;
            flex-direction: column;
            padding: 36px 16px;
            position: fixed;
            height: 100vh;
            left: 0;
            top: 0;
            z-index: 100;
        }
        .sidebar .brand {
            font-family: 'Outfit', sans-serif;
            font-size: 24px;
            font-weight: 700;
            color: var(--accent-purple);
            margin-bottom: 36px;
            padding-left: 16px;
        }
        .nav-menu { list-style: none; flex-grow: 1; }
        .nav-item {
            padding: 14px 20px;
            border-radius: 100px; /* M3 Pill style navigation */
            cursor: pointer;
            color: var(--text-muted);
            font-weight: 500;
            font-size: 14px;
            margin-bottom: 8px;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .nav-item:hover {
            color: var(--text-primary);
            background: rgba(255, 255, 255, 0.05);
        }
        .nav-item.active {
            color: var(--md-sys-color-on-primary-container);
            background: var(--md-sys-color-primary-container);
            font-weight: 600;
        }
        .user-info {
            padding: 20px 16px 0 16px;
            border-top: 1px solid var(--border-color);
            font-size: 13px;
        }
        .user-info .username {
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 6px;
        }
        .user-info .logout {
            color: var(--error-red);
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            margin-top: 8px;
            font-weight: 500;
        }

        /* Main Content */
        .main-content {
            margin-left: 280px;
            flex-grow: 1;
            padding: 48px;
            min-height: 100vh;
            background: var(--bg-color);
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .header {
            margin-bottom: 36px;
        }
        .header h1 {
            font-family: 'Outfit', sans-serif;
            font-size: 32px;
            color: var(--text-primary);
            margin-bottom: 10px;
        }
        .header p { color: var(--text-muted); font-size: 14px; }

        /* Widgets/Grid */
        .widget-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 24px;
            margin-bottom: 40px;
        }
        .card {
            background: var(--card-bg);
            border-radius: 20px; /* M3 Medium card */
            padding: 24px;
            border: 1px solid var(--border-color);
            transition: box-shadow 0.2s ease;
        }
        .card-val {
            font-family: 'Outfit', sans-serif;
            font-size: 32px;
            font-weight: 700;
            color: var(--text-primary);
            margin: 12px 0 6px 0;
            word-break: break-all;
        }
        .card-desc { font-size: 13px; color: var(--text-muted); }

        /* Selector & Table */
        .selector-bar {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 25px;
        }
        .selector-bar select {
            background: var(--md-sys-color-surface-container);
            border: 1px solid var(--md-sys-color-outline);
            color: var(--text-primary);
            padding: 12px 18px;
            border-radius: 12px; /* M3 input */
            font-size: 14px;
            outline: none;
            min-width: 220px;
            transition: border-color 0.2s ease;
        }
        .selector-bar select:focus {
            border-color: var(--accent-purple);
        }
        .table-container {
            background: var(--md-sys-color-surface-container);
            border: 1px solid var(--border-color);
            border-radius: 16px; /* M3 container shape */
            overflow: hidden;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            text-align: left;
        }
        th, td {
            padding: 18px 24px;
            border-bottom: 1px solid var(--border-color);
        }
        th {
            background: rgba(255, 255, 255, 0.03);
            color: var(--text-primary);
            font-weight: 600;
        }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255, 255, 255, 0.01); }
        
        .badge {
            padding: 4px 10px;
            border-radius: 100px; /* Pill badge */
            font-size: 11px;
            font-weight: 600;
        }
        .badge-active { background: rgba(168, 199, 250, 0.15); color: var(--accent-blue); }
        .badge-deleted { background: rgba(255, 180, 171, 0.15); color: var(--error-red); }

        .btn {
            background: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
            border: none;
            border-radius: 100px; /* M3 Pill button */
            padding: 12px 24px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s ease;
        }
        .btn:hover {
            opacity: 0.9;
        }

        .btn-sm {
            padding: 8px 16px;
            border-radius: 100px; /* M3 Pill small button */
            font-size: 12px;
            font-weight: 600;
            border: 1px solid var(--md-sys-color-outline);
            background: transparent;
            color: var(--text-primary);
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn-sm:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: var(--accent-purple);
            color: var(--accent-purple);
        }
        
        .empty-state {
            padding: 48px;
            text-align: center;
            color: var(--text-muted);
            font-size: 14px;
        }

        /* Pagination */
        .pagination-bar {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 8px;
            margin-top: 24px;
        }
        .pagination-btn {
            background: var(--md-sys-color-surface-container);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 10px 18px;
            border-radius: 100px; /* Pill buttons for pagination */
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s ease;
        }
        .pagination-btn:hover:not(:disabled) {
            border-color: var(--accent-purple);
            background: rgba(208, 188, 255, 0.08);
        }
        .pagination-btn.active {
            background: var(--md-sys-color-primary-container);
            border-color: var(--md-sys-color-primary-container);
            color: var(--md-sys-color-on-primary-container);
            font-weight: 600;
        }
        .pagination-btn:disabled {
            opacity: 0.35;
            cursor: not-allowed;
        }

        body.light-theme {
            --md-sys-color-primary: #3f5f90;
            --md-sys-color-on-primary: #ffffff;
            --md-sys-color-primary-container: #d6e3ff;
            --md-sys-color-on-primary-container: #001b3d;
            --md-sys-color-surface: #f9f9fc;
            --md-sys-color-surface-container: #f0f0f4;
            --md-sys-color-surface-container-high: #e4e2e6;
            --md-sys-color-on-surface: #191c20;
            --md-sys-color-on-surface-variant: #43474e;
            --md-sys-color-outline: #73777f;
            --md-sys-color-error: #ba1a1a;
            --md-sys-color-on-error: #ffffff;
            
            --bg-color: var(--md-sys-color-surface);
            --sidebar-bg: var(--md-sys-color-surface-container);
            --card-bg: var(--md-sys-color-surface-container-high);
            --border-color: rgba(115, 119, 127, 0.2);
            --text-primary: var(--md-sys-color-on-surface);
            --text-muted: var(--md-sys-color-on-surface-variant);
            --accent-purple: var(--md-sys-color-primary);
            --accent-blue: #0061a4;
            --accent-green: #006837;
            --error-red: var(--md-sys-color-error);
        }

        .theme-toggle-container {
            margin-top: 15px;
            padding: 0 16px;
        }
        .theme-toggle-btn {
            width: 100%;
            background: var(--md-sys-color-surface-container-high);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 10px 14px;
            border-radius: 100px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: all 0.2s ease;
        }
        .theme-toggle-btn:hover {
            border-color: var(--accent-purple);
            background: rgba(255, 255, 255, 0.05);
        }
        
        /* M3 Dialog Modal Styles */
        .dialog-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(12px);
            z-index: 10000;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.25s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .dialog-overlay.show {
            opacity: 1;
        }
        .dialog-card {
            background: var(--md-sys-color-surface-container-high);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 28px;
            padding: 40px;
            width: 600px;
            min-height: 280px;
            box-shadow: 0 24px 48px rgba(0, 0, 0, 0.45);
            transform: translateY(30px) scale(0.95);
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
            color: var(--text-primary);
            display: flex;
            flex-direction: column;
            gap: 24px;
        }
        /* 라이트 모드일 때 테두리 색상 보강 */
        body.light-theme .dialog-card {
            border: 1px solid rgba(0, 0, 0, 0.08);
            box-shadow: 0 16px 36px rgba(0, 0, 0, 0.15);
        }
        .dialog-overlay.show .dialog-card {
            transform: translateY(0) scale(1);
        }
        .dialog-title {
            font-family: 'Outfit', sans-serif;
            font-size: 26px;
            font-weight: 600;
            color: var(--text-primary);
            margin: 0;
            line-height: 1.3;
            text-align: left;
        }
        .dialog-content {
            font-size: 17px;
            color: var(--text-muted);
            line-height: 1.7;
            text-align: left;
            margin: 0;
            flex-grow: 1;
        }
        .dialog-input-container {
            width: 100%;
            margin: 4px 0;
        }
        .dialog-input {
            width: 100%;
            background: rgba(0, 0, 0, 0.15);
            border: 1px solid var(--md-sys-color-outline);
            color: var(--text-primary);
            padding: 18px 20px;
            border-radius: 16px;
            font-size: 16px;
            outline: none;
            transition: all 0.2s ease;
        }
        body.light-theme .dialog-input {
            background: rgba(0, 0, 0, 0.03);
        }
        .dialog-input:focus {
            border-color: var(--md-sys-color-primary);
            box-shadow: 0 0 0 3px rgba(208, 188, 255, 0.25);
            background: transparent;
        }
        .dialog-actions {
            display: flex;
            justify-content: flex-end;
            gap: 12px;
            width: 100%;
            margin-top: 8px;
        }
        .dialog-btn {
            border: none;
            padding: 12px 32px;
            border-radius: 100px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            height: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .dialog-btn-text {
            background: transparent;
            color: var(--accent-purple);
        }
        .dialog-btn-text:hover {
            background: rgba(208, 188, 255, 0.08);
        }
        .dialog-btn-filled {
            background: var(--error-red);
            color: var(--md-sys-color-on-error);
        }
        .dialog-btn-filled:hover {
            opacity: 0.9;
            box-shadow: 0 4px 12px rgba(255, 180, 171, 0.2);
        }
        .dialog-btn-primary {
            background: var(--md-sys-color-primary);
            color: var(--md-sys-color-on-primary);
        }
        .dialog-btn-primary:hover {
            opacity: 0.9;
            box-shadow: 0 4px 12px rgba(208, 188, 255, 0.2);
        }
        
        /* Toast Notification Styles */
        .toast-container {
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            display: flex;
            flex-direction: column;
            gap: 8px;
            pointer-events: none;
        }
        .toast {
            background: var(--md-sys-color-surface-container-high);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            padding: 12px 24px;
            border-radius: 100px;
            font-size: 13px;
            font-weight: 500;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
            opacity: 0;
            transform: translateY(20px);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            pointer-events: auto;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .toast.show {
            opacity: 1;
            transform: translateY(0);
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="brand">Obsidian Sync</div>
        <ul class="nav-menu" id="sidebarMenu">
            <li class="nav-item active" onclick="switchTab('dashboard')">""")
    parts.append(t('nav.dashboard', lang))
    parts.append("""</li>
            <li class="nav-item" onclick="switchTab('mypage')">""")
    parts.append(t('nav.mypage', lang))
    parts.append("""</li>
            <li class="nav-item" onclick="switchTab('sync')">""")
    parts.append(t('nav.sync', lang))
    parts.append("""</li>
            <li class="nav-item" onclick="switchTab('publish')">""")
    parts.append(t('nav.publish', lang))
    parts.append("""</li>
        </ul>
        <div class="user-info">
            <div class="username" id="sidebarUsername">User Name</div>
            <a class="logout" onclick="logout()">""")
    parts.append(t('nav.logout', lang))
    parts.append("""</a>
            <div class="theme-toggle-container">
                <button class="theme-toggle-btn" id="themeToggleBtn">
                    <span id="themeToggleIcon">☀️</span> <span id="themeToggleText">""")
    parts.append(t('common.theme_light', lang))
    parts.append("""</span>
                </button>
            </div>
        </div>
    </div>

    <div class="main-content">
        <!-- 탭 1: 대시보드 홈 -->
        <div id="tab-dashboard" class="tab-content active">
            <div class="header">
                <h1>""")
    parts.append(t('dashboard.title', lang))
    parts.append("""</h1>
                <p>""")
    parts.append(t('dashboard.desc', lang))
    parts.append("""</p>
            </div>

            <div class="widget-grid">
                <div class="card">
                    <div class="card-desc">""")
    parts.append(t('dashboard.card_vault_count', lang))
    parts.append("""</div>
                    <div class="card-val" id="widgetVaultCount">0</div>
                    <div class="card-desc">""")
    parts.append(t('dashboard.card_vault_count_desc', lang))
    parts.append("""</div>
                </div>
                <div class="card">
                    <div class="card-desc">""")
    parts.append(t('dashboard.card_site_count', lang))
    parts.append("""</div>
                    <div class="card-val" id="widgetSiteCount">0</div>
                    <div class="card-desc">""")
    parts.append(t('dashboard.card_site_count_desc', lang))
    parts.append("""</div>
                </div>

            </div>
        </div>
""")
    parts.append("""
        <!-- 탭 5: 내 정보 관리 (마이페이지) -->
        <div id="tab-mypage" class="tab-content">
            <div class="header">
                <h1>""")
    parts.append(t('mypage.title', lang))
    parts.append("""</h1>
                <p>""")
    parts.append(t('mypage.desc', lang))
    parts.append("""</p>
            </div>

            <div class="card" style="max-width: 600px; background: var(--md-sys-color-surface-container);">
                <div style="font-weight: 600; font-size: 16px; margin-bottom: 20px; color: var(--text-primary);">""")
    parts.append(t('mypage.profile_heading', lang))
    parts.append("""</div>
                <div id="mypageAlert" class="alert alert-error" style="display: none; margin-bottom: 15px;"></div>

                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">""")
    parts.append(t('login.label_username', lang))
    parts.append("""</label>
                    <input type="text" id="mypageUsername" disabled style="width: 100%; background: rgba(0,0,0,0.1); border: 1px solid var(--md-sys-color-outline); color: var(--text-muted); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none; cursor: not-allowed;">
                </div>
                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">""")
    parts.append(t('mypage.label_name', lang))
    parts.append('</label>\n                    <input type="text" id="mypageName" placeholder="')
    parts.append(t('mypage.placeholder_name', lang))
    parts.append("""" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">""")
    parts.append(t('mypage.label_email', lang))
    parts.append("""</label>
                    <input type="email" id="mypageEmail" placeholder="example@example.com" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="margin-bottom: 25px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">""")
    parts.append(t('mypage.label_password', lang))
    parts.append("""</label>
                    <input type="password" id="mypagePassword" placeholder="••••••••" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="display: flex; justify-content: flex-end;">
                    <button class="btn" onclick="updateMyProfile()" style="width: auto;">""")
    parts.append(t('mypage.button_save', lang))
    parts.append("""</button>
                </div>
            </div>

            <div class="card" style="max-width: 600px; background: var(--md-sys-color-surface-container); margin-top: 20px;">
                <div style="font-weight: 600; font-size: 16px; margin-bottom: 20px; color: var(--text-primary);">""")
    parts.append(t('mypage.devices_heading', lang))
    parts.append("""</div>
                <div class="table-container">
                    <table id="myDevicesTable">
                        <thead>
                            <tr>
                                <th>""")
    parts.append(t('devices.col_name', lang))
    parts.append("""</th>
                                <th>""")
    parts.append(t('devices.col_created', lang))
    parts.append("""</th>
                                <th>""")
    parts.append(t('common.col_actions', lang))
    parts.append("""</th>
                            </tr>
                        </thead>
                        <tbody id="myDevicesTableBody">
                            <tr><td colspan="3" class="empty-state">""")
    parts.append(t('common.loading', lang))
    parts.append("""</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 탭 2: Vault 동기화 관리 -->""")
    parts.append("""
        <div id="tab-sync" class="tab-content">
            <div class="header">
                <h1>""")
    parts.append(t('sync.title', lang))
    parts.append("""</h1>
                <p>""")
    parts.append(t('sync.desc', lang))
    parts.append("""</p>
            </div>

            <div class="selector-bar">
                <select id="vaultSelect" onchange="loadVaultFiles()">
                    <option value="">""")
    parts.append(t('sync.select_vault', lang))
    parts.append("""</option>
                </select>
            </div>

            <div class="table-container">
                <table id="filesTable">
                    <thead>
                        <tr>
                            <th>""")
    parts.append(t('sync.col_path', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_size', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('sync.col_modified', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('sync.col_hash', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_status', lang))
    parts.append("""</th>
                        </tr>
                    </thead>
                    <tbody id="filesTableBody">
                        <tr>
                            <td colspan="5" class="empty-state">""")
    parts.append(t('sync.empty_select_vault', lang))
    parts.append("""</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 탭 3: 퍼블리시 관리 -->
        <div id="tab-publish" class="tab-content">
            <div class="header">
                <h1>""")
    parts.append(t('publish.title', lang))
    parts.append("""</h1>
                <p>""")
    parts.append(t('publish.desc', lang))
    parts.append("""</p>
            </div>

            <div class="selector-bar">
                <select id="siteSelect" onchange="loadSiteFiles()">
                    <option value="">""")
    parts.append(t('publish.select_site', lang))
    parts.append("""</option>
                </select>
            </div>

            <div class="table-container">
                <table id="publishTable">
                    <thead>
                        <tr>
                            <th>""")
    parts.append(t('publish.col_path', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_size', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('publish.col_published', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('publish.col_link', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_actions', lang))
    parts.append("""</th>
                        </tr>
                    </thead>
                    <tbody id="publishTableBody">
                        <tr>
                            <td colspan="5" class="empty-state">""")
    parts.append(t('publish.empty_select_site', lang))
    parts.append("""</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 탭 4: 사용자 관리 (관리자 전용) -->
        <div id="tab-users" class="tab-content">
            <div class="header">
                <h1>""")
    parts.append(t('users.title', lang))
    parts.append("""</h1>
                <p>""")
    parts.append(t('users.desc', lang))
    parts.append("""</p>
            </div>

            <!-- 신규 사용자 추가 폼 -->
            <div class="card" style="margin-bottom: 25px; background: var(--md-sys-color-surface-container);">
                <div style="font-weight: 600; font-size: 15px; margin-bottom: 15px; color: var(--text-primary);">""")
    parts.append(t('users.create_heading', lang))
    parts.append('</div>\n                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 15px;">\n                    <input type="text" id="newUsername" placeholder="')
    parts.append(t('users.placeholder_username', lang))
    parts.append('" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">\n                    <input type="password" id="newPassword" placeholder="')
    parts.append(t('users.placeholder_password', lang))
    parts.append('" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">\n                    <input type="text" id="newName" placeholder="')
    parts.append(t('users.placeholder_name', lang))
    parts.append('" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">\n                    <input type="email" id="newEmail" placeholder="')
    parts.append(t('users.placeholder_email', lang))
    parts.append("""" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="display: flex; justify-content: flex-end;">
                    <button class="btn" onclick="createUser()" style="width: auto;">""")
    parts.append(t('users.button_create', lang))
    parts.append("""</button>
                </div>
            </div>

            <div class="table-container">
                <table id="usersTable">""")
    parts.append("""
                    <thead>
                        <tr>
                            <th>""")
    parts.append(t('users.col_id', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('users.col_name', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('users.col_email', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_actions', lang))
    parts.append("""</th>
                        </tr>
                    </thead>
                    <tbody id="usersTableBody">
                        <tr>
                            <td colspan="4" class="empty-state">""")
    parts.append(t('common.loading', lang))
    parts.append("""</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <div id="usersPagination" class="pagination-bar"></div>
        </div>
    </div>

    <script>
        const I18N = """ + json.dumps(get_locale_dict(lang), ensure_ascii=False) + """;
        function t(key, params) {
            let node = I18N;
            for (const part of key.split(".")) {
                if (node == null) break;
                node = node[part];
            }
            let text = (typeof node === "string") ? node : key;
            if (params) {
                text = text.replace(/\\{\\{\\s*([\\w-]+)\\s*\\}\\}/g, (_m, name) => (params[name] !== undefined ? String(params[name]) : ""));
            }
            return text;
        }

        const username = localStorage.getItem("sync_username");
        const isAdmin = localStorage.getItem("sync_is_admin") === "true";
        let currentUsersPage = 1;

        // 테마 토글 기능 구현
        const themeToggleBtn = document.getElementById("themeToggleBtn");
        const themeToggleIcon = document.getElementById("themeToggleIcon");
        const themeToggleText = document.getElementById("themeToggleText");

        function applyTheme(theme) {
            if (theme === "light") {
                document.body.classList.add("light-theme");
                themeToggleIcon.innerText = "🌙";
                themeToggleText.innerText = t("common.theme_dark");
            } else {
                document.body.classList.remove("light-theme");
                themeToggleIcon.innerText = "☀️";
                themeToggleText.innerText = t("common.theme_light");
            }
        }
        
        const savedTheme = localStorage.getItem("theme") || "dark";
        applyTheme(savedTheme);
        
        themeToggleBtn.addEventListener("click", () => {
            const currentTheme = document.body.classList.contains("light-theme") ? "light" : "dark";
            const nextTheme = currentTheme === "light" ? "dark" : "light";
            localStorage.setItem("theme", nextTheme);
            applyTheme(nextTheme);
        });
        
        // No client-readable way to check for a valid session (the cookie is HttpOnly by
        // design) -- an absent/invalid session just surfaces as the first fetchAPI() call
        // getting a 401, which already redirects to /login (see fetchAPI below).
        document.getElementById("sidebarUsername").innerText = username || "Obsidian User";

        // 관리자인 경우 사이드바에 사용자 관리 메뉴 추가
        if (isAdmin) {
            const menu = document.getElementById("sidebarMenu");
            const li = document.createElement("li");
            li.className = "nav-item";
            li.setAttribute("onclick", "switchTab('users')");
            li.innerText = t("nav.users");
            menu.appendChild(li);
        }



        function switchTab(tabId) {
            document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
            document.querySelectorAll(".tab-content").forEach(tab => tab.classList.remove("active"));
            
            // event.currentTarget 대신 직접 탐색하여 active 설정
            const clickedItem = Array.from(document.querySelectorAll(".nav-item")).find(item => {
                const attr = item.getAttribute("onclick");
                return attr && attr.includes(tabId);
            });
            if (clickedItem) clickedItem.classList.add("active");
            
            document.getElementById("tab-" + tabId).classList.add("active");

            if (tabId === 'dashboard') {
                loadDashboardStats();
            } else if (tabId === 'mypage') {
                loadMyProfile();
                loadMyDevices();
            } else if (tabId === 'sync') {
                refreshVaultList();
            } else if (tabId === 'publish') {
                refreshSiteList();
            } else if (tabId === 'users') {
                loadUsersList(1);
            }
        }

        async function logout() {
            try {
                await fetch("/user/logout", { method: "POST" });
            } catch (err) {
                console.error("Logout API failed:", err);
            }
            localStorage.removeItem("sync_username");
            localStorage.removeItem("sync_is_admin");
            window.location.href = "/login";
        }

        async function fetchAPI(url, options = {}) {
            try {
                const response = await fetch(url, options);
                if (response.status === 401) {
                    logout();
                    return null;
                }
                return await response.json();
            } catch (err) {
                console.error("API Error:", err);
                return null;
            }
        }

        async function loadMyProfile() {
            const alertEl = document.getElementById("mypageAlert");
            alertEl.style.display = "none";
            
            try {
                const data = await fetchAPI("/api/user/profile");
                if (data && !data.error) {
                    document.getElementById("mypageUsername").value = data.username || "";
                    document.getElementById("mypageName").value = data.name || "";
                    document.getElementById("mypageEmail").value = data.email || "";
                    document.getElementById("mypagePassword").value = "";
                } else {
                    alertEl.className = "alert alert-error";
                    alertEl.innerText = (data && data.error) ? data.error : t("mypage.msg_load_failed");
                    alertEl.style.display = "block";
                }
            } catch (err) {
                console.error("loadMyProfile error:", err);
                alertEl.className = "alert alert-error";
                alertEl.innerText = t("mypage.msg_load_error_prefix") + err.message;
                alertEl.style.display = "block";
            }
        }

        function showToast(message) {
            const container = document.getElementById("toastContainer");
            if (!container) return;
            
            const toast = document.createElement("div");
            toast.className = "toast";
            toast.innerText = message;
            
            container.appendChild(toast);
            
            // 트리거 애니메이션
            setTimeout(() => toast.classList.add("show"), 10);
            
            // 3초 후 삭제
            setTimeout(() => {
                toast.classList.remove("show");
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }

        async function updateMyProfile() {
            const alertEl = document.getElementById("mypageAlert");
            alertEl.style.display = "none";

            const name = document.getElementById("mypageName").value.trim();
            const email = document.getElementById("mypageEmail").value.trim();
            const password = document.getElementById("mypagePassword").value;

            if (!name || !email) {
                alertEl.className = "alert alert-error";
                alertEl.innerText = t("mypage.msg_name_email_required");
                alertEl.style.display = "block";
                return;
            }

            const res = await fetchAPI("/api/user/profile/update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, email, password })
            });

            if (res && res.ok) {
                alertEl.className = "alert alert-success";
                alertEl.innerText = t("mypage.msg_update_success");
                alertEl.style.display = "block";

                // 로컬 정보 갱신 및 UI 업데이트
                localStorage.setItem("sync_username", name);
                document.getElementById("sidebarUsername").innerText = name;
                document.getElementById("mypagePassword").value = "";
            } else {
                alertEl.className = "alert alert-error";
                alertEl.innerText = res ? res.error : t("mypage.msg_update_failed");
                alertEl.style.display = "block";
            }
        }

        async function loadDashboardStats() {
            const vaultsData = await fetchAPI("/api/admin/vaults");
            const sitesData = await fetchAPI("/api/admin/published");
            
            if (vaultsData) {
                document.getElementById("widgetVaultCount").innerText = vaultsData.vaults.length;
            }
            if (sitesData) {
                document.getElementById("widgetSiteCount").innerText = sitesData.published_sites.length;
            }
        }

        async function refreshVaultList() {
            const data = await fetchAPI("/api/admin/vaults");
            const select = document.getElementById("vaultSelect");
            select.innerHTML = `<option value="">${t("sync.select_vault")}</option>`;
            if (data && data.vaults) {
                data.vaults.forEach(v => {
                    const opt = document.createElement("option");
                    opt.value = v;
                    opt.innerText = v;
                    select.appendChild(opt);
                });
            }
        }

        async function loadVaultFiles() {
            const vaultId = document.getElementById("vaultSelect").value;
            const tbody = document.getElementById("filesTableBody");
            if (!vaultId) {
                tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("sync.empty_select_vault")}</td></tr>`;
                return;
            }
            tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("common.loading")}</td></tr>`;

            const data = await fetchAPI("/api/admin/vaults/" + vaultId);
            tbody.innerHTML = "";
            
            if (data && data.files && data.files.length > 0) {
                data.files.forEach(f => {
                    const tr = document.createElement("tr");
                    
                    const timeStr = new Date(f.modified_at_ms).toLocaleString();
                    const stateBadge = f.is_deleted
                        ? `<span class="badge badge-deleted">${t("sync.badge_deleted")}</span>`
                        : `<span class="badge badge-active">${t("sync.badge_active")}</span>`;

                    tr.innerHTML = `
                        <td>${f.path}</td>
                        <td>${f.size_bytes.toLocaleString()}</td>
                        <td>${timeStr}</td>
                        <td style="font-family: monospace; font-size: 11px;">${f.content_hash || '-'}</td>
                        <td>${stateBadge}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("sync.empty_no_files")}</td></tr>`;
            }
        }

        async function refreshSiteList() {
            const data = await fetchAPI("/api/admin/published");
            const select = document.getElementById("siteSelect");
            select.innerHTML = `<option value="">${t("publish.select_site")}</option>`;
            if (data && data.published_sites) {
                data.published_sites.forEach(s => {
                    const opt = document.createElement("option");
                    opt.value = s.id;
                    opt.setAttribute("data-username", s.username);
                    opt.innerText = `${s.username}/${s.id}`;
                    select.appendChild(opt);
                });
            }
        }

        async function loadSiteFiles() {
            const select = document.getElementById("siteSelect");
            const siteId = select.value;
            const tbody = document.getElementById("publishTableBody");
            if (!siteId) {
                tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("publish.empty_select_site")}</td></tr>`;
                return;
            }
            tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("common.loading")}</td></tr>`;

            const data = await fetchAPI("/api/list", {
                method: "POST",
                headers: { "Content-Type": "application/json", "obs-id": siteId }
            });

            tbody.innerHTML = "";
            if (data && data.files && data.files.length > 0) {
                data.files.forEach(f => {
                    const tr = document.createElement("tr");
                    const timeStr = f.mtime ? new Date(f.mtime).toLocaleString() : "-";
                    const selectedOpt = select.options[select.selectedIndex];
                    const username = selectedOpt ? selectedOpt.getAttribute("data-username") : "default_user";
                    const pubLink = `/publish/${username}/${siteId}/${f.path}`;

                    tr.innerHTML = `
                        <td>${f.path}</td>
                        <td>${f.size.toLocaleString()}</td>
                        <td>${timeStr}</td>
                        <td><a href="${pubLink}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">${t("publish.link_open")}</a></td>
                        <td><button class="btn-sm" onclick="unpublishPage('${siteId}', '${f.path}')">${t("publish.button_unpublish")}</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${t("publish.empty_no_pages")}</td></tr>`;
            }
        }

        async function unpublishPage(siteId, path) {
            const confirmed = await showM3Confirm(
                t("publish.confirm_title"),
                t("publish.confirm_content", { path }),
                t("publish.confirm_button"),
                true,
                "warning"
            );
            if (!confirmed) return;

            const res = await fetchAPI("/api/remove", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: siteId, path: path })
            });

            if (res && res.ok) {
                showToast(t("publish.msg_remove_success"));
                loadSiteFiles();
            } else {
                showToast(t("publish.msg_remove_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        // 사용자 목록 로드 (관리자 전용)
        async function loadUsersList(page = 1) {
            currentUsersPage = page;
            const tbody = document.getElementById("usersTableBody");
            const paginationDiv = document.getElementById("usersPagination");
            tbody.innerHTML = `<tr><td colspan="4" class="empty-state">${t("common.loading")}</td></tr>`;
            if (paginationDiv) paginationDiv.innerHTML = "";
            
            const limit = 10;
            const data = await fetchAPI(`/api/admin/users?page=${page}&limit=${limit}`);
            tbody.innerHTML = "";
            
            if (data && data.users && data.users.length > 0) {
                data.users.forEach(u => {
                    const tr = document.createElement("tr");
                    
                    // 본인 계정은 삭제 불가능하게 방지
                    const isSelf = u.username === username;
                    const deleteBtnHtml = isSelf
                        ? `<span class="badge badge-active">${t("users.badge_self")}</span>`
                        : `<button class="btn-sm" onclick="deleteUser('${u.username}')" style="margin-right: 8px;">${t("common.button_delete")}</button>`;

                    const resetPwBtnHtml = `<button class="btn-sm" onclick="resetUserPassword('${u.username}')" style="border-color: var(--accent-blue); color: var(--accent-blue); background: transparent; margin-right: 8px;">${t("users.button_reset_password")}</button>`;
                    const devicesBtnHtml = `<button class="btn-sm" onclick="openDeviceModal('${u.username}')" style="border-color: var(--accent-green); color: var(--accent-green); background: transparent;">${t("devices.button_manage")}</button>`;

                    tr.innerHTML = `
                        <td>${u.username}</td>
                        <td>${u.name || '-'}</td>
                        <td>${u.email || '-'}</td>
                        <td>
                            ${deleteBtnHtml}
                            ${resetPwBtnHtml}
                            ${devicesBtnHtml}
                        </td>
                    `;
                    tbody.appendChild(tr);
                });

                if (data.total && data.total > limit) {
                    const totalPages = Math.ceil(data.total / limit);
                    renderPagination(totalPages, page);
                }
            } else {
                tbody.innerHTML = `<tr><td colspan="4" class="empty-state">${t("users.empty_no_users")}</td></tr>`;
            }
        }

        function renderPagination(totalPages, currentPage) {
            const paginationDiv = document.getElementById("usersPagination");
            if (!paginationDiv) return;
            paginationDiv.innerHTML = "";

            // 이전 버튼
            const prevBtn = document.createElement("button");
            prevBtn.className = "pagination-btn";
            prevBtn.innerText = t("common.button_prev");
            prevBtn.disabled = currentPage === 1;
            prevBtn.onclick = () => loadUsersList(currentPage - 1);
            paginationDiv.appendChild(prevBtn);

            // 페이지 번호 버튼들
            for (let i = 1; i <= totalPages; i++) {
                const btn = document.createElement("button");
                btn.className = `pagination-btn ${i === currentPage ? 'active' : ''}`;
                btn.innerText = i;
                btn.onclick = () => loadUsersList(i);
                paginationDiv.appendChild(btn);
            }

            // 다음 버튼
            const nextBtn = document.createElement("button");
            nextBtn.className = "pagination-btn";
            nextBtn.innerText = t("common.button_next");
            nextBtn.disabled = currentPage === totalPages;
            nextBtn.onclick = () => loadUsersList(currentPage + 1);
            paginationDiv.appendChild(nextBtn);
        }

        function showM3Confirm(title, content, confirmText = t("common.button_delete"), isDestructive = true) {
            return new Promise((resolve) => {
                const overlay = document.getElementById("m3DialogOverlay");
                const titleEl = document.getElementById("dialogTitle");
                const contentEl = document.getElementById("dialogContent");
                const confirmBtn = document.getElementById("dialogConfirmBtn");
                const cancelBtn = document.getElementById("dialogCancelBtn");

                titleEl.innerText = title;
                contentEl.innerHTML = content.replace(new RegExp("\\\\n", "g"), "<br>").replace(new RegExp("\\n", "g"), "<br>");
                confirmBtn.innerText = confirmText;

                if (isDestructive) {
                    confirmBtn.className = "dialog-btn dialog-btn-filled";
                } else {
                    confirmBtn.className = "dialog-btn dialog-btn-primary";
                }

                overlay.style.display = "flex";
                setTimeout(() => overlay.classList.add("show"), 10);

                function cleanup(result) {
                    overlay.classList.remove("show");
                    setTimeout(() => {
                        overlay.style.display = "none";
                    }, 250);
                    confirmBtn.removeEventListener("click", onConfirm);
                    cancelBtn.removeEventListener("click", onCancel);
                    window.removeEventListener("keydown", onKeydown);
                    resolve(result);
                }

                function onConfirm() { cleanup(true); }
                function onCancel() { cleanup(false); }

                function onKeydown(e) {
                    if (e.key === "Escape" || e.keyCode === 27) {
                        onCancel();
                    }
                }

                confirmBtn.addEventListener("click", onConfirm);
                cancelBtn.addEventListener("click", onCancel);
                window.addEventListener("keydown", onKeydown);
            });
        }

        function showM3Prompt(title, content, placeholder = "") {
            return new Promise((resolve) => {
                const overlay = document.getElementById("m3DialogOverlay");
                const titleEl = document.getElementById("dialogTitle");
                const contentEl = document.getElementById("dialogContent");
                const inputContainer = document.getElementById("dialogInputContainer");
                const inputEl = document.getElementById("dialogInput");
                const confirmBtn = document.getElementById("dialogConfirmBtn");
                const cancelBtn = document.getElementById("dialogCancelBtn");

                titleEl.innerText = title;
                contentEl.innerHTML = content.replace(new RegExp("\\\\n", "g"), "<br>").replace(new RegExp("\\n", "g"), "<br>");
                inputEl.placeholder = placeholder;
                inputEl.value = "";

                inputContainer.style.display = "block";
                confirmBtn.innerText = t("common.button_change");
                confirmBtn.className = "dialog-btn dialog-btn-primary";

                overlay.style.display = "flex";
                setTimeout(() => {
                    overlay.classList.add("show");
                    inputEl.focus();
                }, 10);

                function cleanup(resultValue) {
                    overlay.classList.remove("show");
                    setTimeout(() => {
                        overlay.style.display = "none";
                        inputContainer.style.display = "none";
                    }, 250);
                    confirmBtn.removeEventListener("click", onConfirm);
                    cancelBtn.removeEventListener("click", onCancel);
                    window.removeEventListener("keydown", onKeydown);
                    resolve(resultValue);
                }

                function onConfirm() {
                    cleanup(inputEl.value);
                }
                function onCancel() {
                    cleanup(null);
                }

                function onKeydown(e) {
                    if (e.key === "Escape" || e.keyCode === 27) {
                        onCancel();
                    } else if (e.key === "Enter" || e.keyCode === 13) {
                        onConfirm();
                    }
                }

                confirmBtn.addEventListener("click", onConfirm);
                cancelBtn.addEventListener("click", onCancel);
                window.addEventListener("keydown", onKeydown);
            });
        }

        async function deleteUser(targetUsername) {
            const confirmed = await showM3Confirm(
                t("users.confirm_delete_title"),
                t("users.confirm_delete_content", { username: targetUsername }),
                t("common.button_delete"),
                true
            );
            if (!confirmed) return;

            const res = await fetchAPI("/api/admin/users/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: targetUsername })
            });

            if (res && res.ok) {
                showToast(t("users.msg_delete_success"));
                loadUsersList(currentUsersPage);
            } else {
                showToast(t("users.msg_delete_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        async function resetUserPassword(targetUsername) {
            const newPassword = await showM3Prompt(
                t("users.prompt_reset_password_title"),
                t("users.prompt_reset_password_content", { username: targetUsername }),
                t("users.placeholder_new_password")
            );
            if (newPassword === null) return; // 취소
            if (!newPassword.trim()) {
                showToast(t("users.msg_password_empty"));
                return;
            }

            const res = await fetchAPI("/api/admin/users/reset-password", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: targetUsername, password: newPassword })
            });

            if (res && res.ok) {
                showToast(t("users.msg_password_changed"));
            } else {
                showToast(t("users.msg_password_change_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        async function createUser() {
            const usernameInput = document.getElementById("newUsername");
            const passwordInput = document.getElementById("newPassword");
            const nameInput = document.getElementById("newName");
            const emailInput = document.getElementById("newEmail");

            const newUsername = usernameInput.value.trim();
            const newPassword = passwordInput.value.trim();
            const newName = nameInput.value.trim();
            const newEmail = emailInput.value.trim();

            if (!newUsername || !newPassword) {
                showToast(t("users.msg_create_missing_fields"));
                return;
            }

            const res = await fetchAPI("/api/admin/users/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username: newUsername,
                    password: newPassword,
                    name: newName,
                    email: newEmail
                })
            });
            
            if (res && res.ok) {
                showToast(t("users.msg_create_success", { username: newUsername }));
                usernameInput.value = "";
                passwordInput.value = "";
                nameInput.value = "";
                emailInput.value = "";
                loadUsersList(1);
            } else {
                showToast(t("users.msg_create_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        // 디바이스(gRPC 동기화 토큰) 관리 -- 마이페이지의 "내 디바이스" 목록과 관리자의 사용자별
        // 디바이스 관리 모달이 동일한 로직을 공유한다 (조회 URL/폐기 URL만 다름).
        async function loadDeviceList(tbodyId, apiUrl, revokeUrl) {
            const tbody = document.getElementById(tbodyId);
            tbody.innerHTML = `<tr><td colspan="3" class="empty-state">${t("common.loading")}</td></tr>`;

            const data = await fetchAPI(apiUrl);
            tbody.innerHTML = "";

            if (data && data.devices && data.devices.length > 0) {
                data.devices.forEach(d => {
                    const tr = document.createElement("tr");
                    const created = d.created_at_ms ? new Date(d.created_at_ms).toLocaleString() : "-";
                    tr.innerHTML = `
                        <td>${d.device_name || t("devices.unnamed")}</td>
                        <td>${created}</td>
                        <td><button class="btn-sm" onclick="revokeDevice('${revokeUrl}', '${d.token}', '${tbodyId}', '${apiUrl}')" style="border-color: var(--error-red); color: var(--error-red); background: transparent;">${t("devices.button_revoke")}</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = `<tr><td colspan="3" class="empty-state">${t("devices.empty")}</td></tr>`;
            }
        }

        async function revokeDevice(revokeUrl, token, tbodyId, apiUrl) {
            const confirmed = await showM3Confirm(
                t("devices.confirm_title"),
                t("devices.confirm_content"),
                t("devices.button_revoke"),
                true
            );
            if (!confirmed) return;

            const res = await fetchAPI(revokeUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token })
            });

            if (res && res.ok) {
                showToast(t("devices.msg_revoke_success"));
                loadDeviceList(tbodyId, apiUrl, revokeUrl);
            } else {
                showToast(t("devices.msg_revoke_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        function loadMyDevices() {
            loadDeviceList("myDevicesTableBody", "/api/user/devices", "/api/user/devices/delete");
        }

        function openDeviceModal(targetUsername) {
            document.getElementById("deviceModalUsername").innerText = targetUsername;
            const overlay = document.getElementById("deviceModalOverlay");
            overlay.style.display = "flex";
            setTimeout(() => overlay.classList.add("show"), 10);
            loadDeviceList(
                "deviceModalTableBody",
                `/api/admin/users/${encodeURIComponent(targetUsername)}/devices`,
                `/api/admin/users/${encodeURIComponent(targetUsername)}/devices/delete`
            );
        }

        function closeDeviceModal() {
            const overlay = document.getElementById("deviceModalOverlay");
            overlay.classList.remove("show");
            setTimeout(() => { overlay.style.display = "none"; }, 250);
        }

        // 초기 로드
        loadDashboardStats();
    </script>
    <div id="deviceModalOverlay" class="dialog-overlay" style="display: none;">
        <div class="dialog-card" style="width: 640px;">
            <h2 class="dialog-title">""")
    parts.append(t('devices.button_manage', lang))
    parts.append(""" (<span id="deviceModalUsername"></span>)</h2>
            <div class="dialog-content" style="max-height: 360px; overflow-y: auto;">
                <table id="deviceModalTable">
                    <thead>
                        <tr>
                            <th>""")
    parts.append(t('devices.col_name', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('devices.col_created', lang))
    parts.append("""</th>
                            <th>""")
    parts.append(t('common.col_actions', lang))
    parts.append("""</th>
                        </tr>
                    </thead>
                    <tbody id="deviceModalTableBody">
                        <tr><td colspan="3" class="empty-state">""")
    parts.append(t('common.loading', lang))
    parts.append("""</td></tr>
                    </tbody>
                </table>
            </div>
            <div class="dialog-actions">
                <button class="dialog-btn dialog-btn-text" onclick="closeDeviceModal()">""")
    parts.append(t('common.button_close', lang))
    parts.append("""</button>
            </div>
        </div>
    </div>
    <div id="m3DialogOverlay" class="dialog-overlay" style="display: none;">
        <div class="dialog-card">
            <h2 class="dialog-title" id="dialogTitle">""")
    parts.append(t('common.dialog_title_default', lang))
    parts.append("""</h2>
            <div class="dialog-content" id="dialogContent">""")
    parts.append(t('common.dialog_content_default', lang))
    parts.append('</div>\n            <div id="dialogInputContainer" class="dialog-input-container" style="display: none;">\n                <input type="password" id="dialogInput" class="dialog-input" placeholder="')
    parts.append(t('users.placeholder_new_password', lang))
    parts.append("""">
            </div>
            <div class="dialog-actions">
                <button class="dialog-btn dialog-btn-text" id="dialogCancelBtn">""")
    parts.append(t('common.button_cancel', lang))
    parts.append("""</button>
                <button class="dialog-btn dialog-btn-filled" id="dialogConfirmBtn">""")
    parts.append(t('common.button_confirm', lang))
    parts.append("""</button>
            </div>
        </div>
    </div>
    <div id="toastContainer" class="toast-container"></div>
</body>
</html>""")
    html = "".join(parts)
    return Response(html, content_type="text/html")

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

# 1. API for looking up a file's backup version history
@view_config(route_name='get_history', renderer='json', permission='vault-access')
def get_history_view(request):
    vault_id = request.context.vault_id
    path = request.params.get("path")
    if not path:
        return Response(body=json.dumps({"error": "Missing vault_id or path"}).encode("utf-8"), status=400, content_type="application/json")

    try:
        history_rows = request.repository.get_history(request.context.owner, vault_id, path)
        versions = []
        for row in history_rows:
            versions.append({
                "history_id": row["history_id"],
                "modified_at_ms": row["modified_at_ms"],
                "size_bytes": row["size_bytes"],
                "content_hash": row["content_hash"],
                "device_name": row.get("device_name", "Unknown Device"),
                "user_name": row.get("user_name", "Unknown User"),
                "deleted": bool(row.get("deleted", False)),
                "related_path": row.get("related_path"),
            })
        return {"versions": versions}
    except Exception as e:
        logger.error(f"HTTP get_history failed for {path}: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

# 2. API for downloading a specific version's file (binary response)
@view_config(route_name='download_history', permission='vault-access')
def download_history_view(request):
    history_id_str = request.params.get("history_id")
    if not history_id_str:
        return Response("Missing vault_id or history_id", status=400)

    try:
        history_id = int(history_id_str)
        row = request.repository.get_history_by_id(request.context.owner, history_id)
        if not row:
            return Response("History version not found", status=404)
            
        backup_file_path = row["backup_file_path"]
        if not os.path.exists(backup_file_path):
            return Response("Backup file not found on disk", status=404)
            
        with open(backup_file_path, 'rb') as f:
            file_data = f.read()
            
        response = Response(body=file_data, content_type="application/octet-stream")
        response.headers["X-File-Path"] = quote(row["path"])
        response.headers["X-File-Size"] = str(row["size_bytes"])
        response.headers["X-File-Mtime"] = str(row["modified_at_ms"])
        return response
    except Exception as e:
        logger.error(f"HTTP download_history failed: {e}")
        return Response(str(e), status=500)

# 3. API for restoring a server-side file to a specific version (POST)
@view_config(route_name='restore_history', renderer='json', permission='vault-access')
def restore_history_view(request):
    try:
        vault_id = request.context.vault_id
        body = request.json_body
        history_id = body.get("history_id")
        req_path = body.get("path", "")

        if history_id is None:
            return Response(body=json.dumps({"error": "Missing vault_id or history_id"}).encode("utf-8"), status=400, content_type="application/json")

        row = request.repository.get_history_by_id(request.context.owner, history_id)
        if not row:
            return {"ok": False, "error": "History version not found"}

        backup_file_path = row["backup_file_path"]
        if not os.path.exists(backup_file_path):
            return {"ok": False, "error": "Backup file not found on disk"}

        target_path = req_path if req_path else row["path"]

        data_dir = request.registry.settings.get("data_dir")
        vault_path = get_vault_path(data_dir, request.context.owner, vault_id)
        dest_file_path = os.path.join(vault_path, target_path)
        
        if not dest_file_path.startswith(os.path.abspath(vault_path)):
            return {"ok": False, "error": "Invalid target path (escape attempt)"}
            
        os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
        if os.path.exists(dest_file_path):
            os.remove(dest_file_path)
        shutil.copy2(backup_file_path, dest_file_path)
        
        current_mtime_ms = int(time.time() * 1000)
        mtime_sec = current_mtime_ms / 1000.0
        os.utime(dest_file_path, (mtime_sec, mtime_sec))
        
        meta = {
            "path": target_path,
            "modified_at_ms": current_mtime_ms,
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "is_deleted": False
        }
        request.repository.save_one(request.context.owner, vault_id, target_path, meta)

        try:
            device_name = unquote(request.headers.get("X-Device-Name", "Unknown Device"))
            user_name = unquote(request.headers.get("X-User-Name", "Unknown User"))

            request.repository.add_history(
                owner_username=request.context.owner,
                vault_id=vault_id,
                path=target_path,
                modified_at_ms=current_mtime_ms,
                size_bytes=row["size_bytes"],
                content_hash=row["content_hash"],
                backup_file_path=backup_file_path,
                device_name=device_name,
                user_name=user_name
            )
            logger.info(f"Restore version history added for {target_path} referencing {backup_file_path}")
        except Exception as backup_err:
            logger.error(f"Failed to record restore version history for {target_path}: {backup_err}")
            
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP restore_history failed: {e}")
        return {"ok": False, "error": str(e)}

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

def get_beautiful_html(username: str, vault_id: str, path: str, html_content: str) -> str:
    title = os.path.basename(path)
    if title.endswith(".md"):
        title = title[:-3]
        
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - {vault_id} Publish</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0d1117;
            --container-bg: rgba(22, 27, 34, 0.7);
            --border-color: rgba(48, 54, 61, 0.8);
            --text-primary: #c9d1d9;
            --text-muted: #8b949e;
            --accent-purple: #8ab4f8;
            --accent-blue: #58a6ff;
            --wiki-link: #a371f7;
            --wiki-link-hover: #b893f9;
            --code-bg: #161b22;
        }}
        
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        
        body {{
            background: radial-gradient(circle at 50% 0%, #1c1d2e 0%, #0d1117 70%);
            color: var(--text-primary);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            line-height: 1.7;
            min-height: 100vh;
            padding: 40px 20px;
        }}
        
        .layout {{
            max-width: 900px;
            margin: 0 auto;
        }}
        
        header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 40px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }}
        
        .site-title {{
            font-family: 'Outfit', sans-serif;
            font-size: 24px;
            font-weight: 800;
            background: linear-gradient(135deg, var(--accent-blue), var(--wiki-link));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-decoration: none;
            letter-spacing: -0.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .site-title::before {{
            content: "🚀";
            font-size: 20px;
        }}
        
        .vault-badge {{
            background: rgba(138, 180, 248, 0.1);
            color: var(--accent-purple);
            border: 1px solid rgba(138, 180, 248, 0.2);
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 500;
        }}
        
        .container {{
            background: var(--container-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 50px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
        }}
        
        .markdown-body h1 {{
            font-family: 'Outfit', sans-serif;
            font-size: 2.2em;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 24px;
            color: #ffffff;
            border-bottom: 1px solid rgba(48, 54, 61, 0.4);
            padding-bottom: 12px;
        }}
        
        .markdown-body h2 {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.6em;
            margin-top: 36px;
            margin-bottom: 16px;
            color: #f0f6fc;
            display: flex;
            align-items: center;
        }}
        
        .markdown-body h2::before {{
            content: "#";
            color: var(--accent-blue);
            margin-right: 8px;
            font-weight: 300;
            font-size: 0.9em;
        }}
        
        .markdown-body h3 {{
            font-size: 1.3em;
            margin-top: 24px;
            margin-bottom: 12px;
            color: #f0f6fc;
        }}
        
        .markdown-body p {{
            margin-bottom: 20px;
            color: var(--text-primary);
        }}
        
        /* Links and Wiki-links styling */
        .markdown-body a {{
            color: var(--accent-blue);
            text-decoration: none;
            transition: color 0.2s ease, border-bottom-color 0.2s ease;
            border-bottom: 1px dashed rgba(88, 166, 255, 0.4);
        }}
        
        .markdown-body a:hover {{
            color: #79c0ff;
            border-bottom-color: #79c0ff;
        }}
        
        .markdown-body a.wiki-link {{
            color: var(--wiki-link);
            border-bottom: 1px solid rgba(163, 113, 247, 0.4);
            font-weight: 500;
            padding: 1px 2px;
            border-radius: 4px;
            background: rgba(163, 113, 247, 0.05);
            transition: all 0.2s ease;
        }}
        
        .markdown-body a.wiki-link:hover {{
            color: var(--wiki-link-hover);
            background: rgba(163, 113, 247, 0.12);
            border-bottom-color: var(--wiki-link-hover);
            transform: translateY(-1px);
        }}
        
        /* Lists */
        .markdown-body ul, .markdown-body ol {{
            margin-bottom: 20px;
            padding-left: 24px;
        }}
        
        .markdown-body li {{
            margin-bottom: 8px;
        }}
        
        /* Code blocks */
        .markdown-body code {{
            font-family: 'Fira Code', monospace;
            background-color: rgba(110, 118, 129, 0.2);
            padding: 3px 6px;
            border-radius: 6px;
            font-size: 85%;
        }}
        
        .markdown-body pre {{
            background-color: var(--code-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            overflow-x: auto;
            margin-bottom: 20px;
        }}
        
        .markdown-body pre code {{
            background-color: transparent;
            padding: 0;
            font-size: 90%;
            color: #e6edf3;
            border-radius: 0;
        }}
        
        /* Images */
        .markdown-body img {{
            max-width: 100%;
            height: auto;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            margin: 20px 0;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            display: block;
        }}
        
        /* Blockquote */
        .markdown-body blockquote {{
            border-left: 4px solid var(--wiki-link);
            padding-left: 20px;
            color: var(--text-muted);
            font-style: italic;
            margin: 24px 0;
            background: rgba(163, 113, 247, 0.03);
            border-radius: 0 8px 8px 0;
            padding-top: 10px;
            padding-bottom: 10px;
        }}
        
        /* Table */
        .markdown-body table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 24px;
        }}
        
        .markdown-body th, .markdown-body td {{
            border: 1px solid var(--border-color);
            padding: 12px 16px;
            text-align: left;
        }}
        
        .markdown-body th {{
            background-color: rgba(138, 180, 248, 0.05);
            color: #ffffff;
            font-weight: 600;
        }}
        
        .markdown-body tr:nth-child(even) {{
            background-color: rgba(255, 255, 255, 0.02);
        }}
        
        footer {{
            margin-top: 50px;
            text-align: center;
            font-size: 13px;
            color: var(--text-muted);
        }}
        
        footer a {{
            color: var(--text-primary);
            text-decoration: none;
        }}
        
        footer a:hover {{
            text-decoration: underline;
        }}
        
        @media (max-width: 768px) {{
            .container {{
                padding: 24px;
                border-radius: 12px;
            }}
            body {{
                padding: 20px 10px;
            }}
        }}
    </style>
</head>
<body>
    <div class="layout">
        <header>
            <a href="/publish/{username}/{vault_id}/" class="site-title">{vault_id} Publish</a>
            <span class="vault-badge">Vault: {vault_id}</span>
        </header>
        <main class="container markdown-body">
            {html_content}
        </main>
        <footer>
            <p>Powered by Antigravity Obsidian Publish Server | <a href="/publish/{username}/{vault_id}/index.md">Index</a></p>
        </footer>
    </div>
</body>
</html>
"""

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
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_upload failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

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

def get_index_html(username: str, vault_id: str, files: list) -> str:
    items = "".join(
        f'<li><a href="/publish/{quote(username, safe="")}/{quote(vault_id, safe="")}/{quote(f, safe="/")}">{f}</a></li>'
        for f in sorted(files)
    )
    return get_beautiful_html(username, vault_id, "", f"<h1>{vault_id}</h1><ul>{items}</ul>" if items else f"<h1>{vault_id}</h1><p>게시된 파일이 없습니다.</p>")

@view_config(route_name='publish_view', permission=NO_PERMISSION_REQUIRED)
def publish_view(request):
    username = request.matchdict.get("username")
    vault_id = request.matchdict.get("vault_id")
    path = request.matchdict.get("path", "")

    data_dir = request.registry.settings.get("data_dir")
    published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))

    # Root: show the list of published files
    if not path:
        files = []
        if os.path.isdir(published_base):
            for root, _, fnames in os.walk(published_base):
                for fname in fnames:
                    rel = os.path.relpath(os.path.join(root, fname), published_base)
                    files.append(rel.replace(os.sep, "/"))
        return Response(get_index_html(username, vault_id, files), content_type="text/html")

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
    
    if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp4", ".mp3", ".wav"]:
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
            ".mp4": "video/mp4",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav"
        }
        content_type = mime_types.get(ext, "application/octet-stream")
        with open(file_path, "rb") as f:
            return Response(body=f.read(), content_type=content_type)
            
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
    except Exception as e:
        return Response(f"Failed to read file: {e}", status=500)
        
    processed_md = parse_wikilinks(strip_frontmatter(md_content), username, vault_id)
    
    import markdown
    html_content = markdown.markdown(processed_md, extensions=['extra', 'codehilite', 'toc'])
    full_html = get_beautiful_html(username, vault_id, path, html_content)
    
    return Response(full_html, content_type="text/html")


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
        "data_dir": data_dir
    }
    # Initialize the Pyramid Configurator
    config = Configurator(settings=settings)

    # Register a helper attribute so views can reach the DB repository via request.repository
    config.add_request_method(lambda r: r.registry.settings["repository"], "repository", reify=True)

    # Add tweens: a tween added later runs on the outermost layer
    config.add_tween("server.web.cors_tween_factory")

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
    config.add_route('user_device_delete', '/api/user/devices/delete')
    config.add_route('admin_users', '/api/admin/users')
    config.add_route('admin_user_create', '/api/admin/users/create')
    config.add_route('admin_user_delete', '/api/admin/users/delete')
    config.add_route('admin_user_reset_password', '/api/admin/users/reset-password')
    config.add_route('admin_user_devices', '/api/admin/users/{username}/devices')
    config.add_route('admin_user_device_delete', '/api/admin/users/{username}/devices/delete')
    config.add_route('admin_vaults', '/api/admin/vaults')
    config.add_route('admin_vault_files', '/api/admin/vaults/{vault_id}', factory=VaultContext)
    config.add_route('admin_published', '/api/admin/published')
    config.add_route('get_token_info', '/api/token/info')
    config.add_route('get_history', '/api/history', factory=VaultContext)
    config.add_route('download_history', '/api/history/download', factory=VaultContext)
    config.add_route('restore_history', '/api/history/restore', factory=VaultContext)
    config.add_route('publish_list', '/api/list', factory=VaultContext)
    config.add_route('publish_upload', '/api/upload', factory=VaultContext)
    config.add_route('publish_remove', '/api/remove', factory=VaultContext)
    config.add_route('publish_download', '/api/download', factory=VaultContext)
    config.add_route('publish_slugs', '/api/slugs')
    config.add_route('publish_site', '/api/site')
    config.add_route('publish_customurl', '/api/customurl')
    config.add_route('publish_slug', '/api/slug', factory=VaultContext)
    config.add_route('publish_password', '/api/password', factory=VaultContext)
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
