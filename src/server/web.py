import time
import json
import logging
import re
import os
import shutil
import uuid
from urllib.parse import quote, unquote
from pyramid.config import Configurator
from pyramid.response import Response
from pyramid.view import view_config

logger = logging.getLogger("server.web")

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
def token_auth_tween_factory(handler, registry):
    def token_auth_tween(request):
        # /api/ping, /publish/, and the publish API paths are exempt from the token check
        # (allows the ad-hoc obs-token issued at official account login to bypass it)
        bypass_paths = [
            "/api/ping", "/api/list", "/api/upload", "/api/remove",
            "/api/slugs", "/api/site", "/api/customurl", "/api/slug", "/api/password",
            "/api/download", "/user/login", "/login", "/dashboard"
        ]
        if request.path in bypass_paths or request.path.startswith("/publish/"):
            return handler(request)

        auth_header = request.headers.get("Authorization", "")
        obs_token = request.headers.get("obs-token", "")

        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif obs_token:
            token = obs_token

        # There's no separate master/bypass token anymore -- every caller authenticates as a
        # real user via their web-dashboard token (users.token).
        user = request.repository.get_user_by_token(token) if request.repository else None

        if not user:
            logger.warning(f"Unauthorized HTTP access attempt: {request.method} {request.path}")
            return Response(
                body=json.dumps({"error": "Unauthorized"}).encode("utf-8"),
                status=401,
                content_type="application/json"
            )
            
        return handler(request)
    return token_auth_tween

def get_authenticated_user(request):
    auth_header = request.headers.get("Authorization", "")
    obs_token = request.headers.get("obs-token", "")
    
    # Extract a body token for APIs that take the token in the JSON body (e.g. unpublish)
    body_token = ""
    try:
        if request.body:
            body = request.json_body
            body_token = body.get("token")
    except Exception:
        pass

    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif obs_token:
        token = obs_token.strip()
    elif body_token:
        token = body_token.strip()
        
    if not token:
        return None

    # There's no separate master/bypass token anymore -- every caller authenticates as a real
    # user via their web-dashboard token (users.token). Whichever account's username matches
    # ADMIN_USER is treated as admin.
    user = request.repository.get_user_by_token(token) if request.repository else None
    if user:
        admin_user = os.getenv("ADMIN_USER")
        is_admin = (user["username"] == admin_user) if admin_user else False
        return {"username": user["username"], "is_admin": is_admin}
        
    return None

def verify_vault_ownership(request, vault_id):
    if not vault_id:
        return None
    user_info = get_authenticated_user(request)
    if not user_info:
        return None  # Unauthorized
    # No admin bypass, intentionally: vault content is private to its owner. Being admin grants
    # account-management capabilities, not access to other people's notes.
    owner = request.repository.get_vault_owner(vault_id)
    if not owner:
        request.repository.set_vault_owner(vault_id, user_info["username"])
        logger.info(f"Set owner of vault '{vault_id}' to '{user_info['username']}'")
        return user_info
    if owner != user_info["username"]:
        return False  # Forbidden
    return user_info

# Helper that resolves a vault's absolute physical storage path
def get_vault_path(data_dir: str, vault_id: str) -> str:
    vaults_dir = os.path.join(data_dir, "vaults")
    vault_path = os.path.abspath(os.path.join(vaults_dir, vault_id))
    if not vault_path.startswith(os.path.abspath(vaults_dir)):
        raise ValueError("Invalid vault ID")
    return vault_path

# Helper that resolves the publish metadata directory
def get_publish_meta_dir(data_dir: str, vault_id: str) -> str:
    d = os.path.join(data_dir, "publish_meta", vault_id)
    os.makedirs(d, exist_ok=True)
    return d

def load_publish_meta(data_dir: str, vault_id: str, filename: str, default=None):
    meta_dir = get_publish_meta_dir(data_dir, vault_id)
    path = os.path.join(meta_dir, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

def save_publish_meta(data_dir: str, vault_id: str, filename: str, data):
    meta_dir = get_publish_meta_dir(data_dir, vault_id)
    path = os.path.join(meta_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

@view_config(route_name='ping', renderer='json')
def ping_view(request):
    return {
        "status": "ok",
        "message": "Obsidian Sync HTTP Portal (Pyramid) is running on Twisted reactor",
        "timestamp_ms": int(time.time() * 1000)
    }

@view_config(route_name='login_page')
def login_page_view(request):
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Obsidian Sync - Login</title>
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
        <span id="themeToggleIcon">☀️</span> <span id="themeToggleText">라이트 모드</span>
    </button>

    <div class="login-card">
        <div class="header">
            <div class="logo">Obsidian Sync</div>
            <div class="subtitle" id="cardSubtitle">계정 로그인 및 토큰 발급</div>
        </div>
        <div id="alert" class="alert alert-error"></div>
        <form id="loginForm">
            <div class="form-group">
                <label for="username">아이디 (Username)</label>
                <input type="text" id="username" required placeholder="아이디를 입력하세요">
            </div>
            <div class="form-group">
                <label for="password">비밀번호</label>
                <input type="password" id="password" required placeholder="••••••••">
            </div>

            <button type="submit" class="btn" id="submitBtn">로그인</button>
        </form>

        <div style="text-align: center; margin-top: 20px; font-size: 13px; color: var(--md-sys-color-on-surface-variant);">
            계정이 없으신가요? 관리자에게 문의해 계정을 발급받으세요.
        </div>

        <div id="resultBox" class="result-box">
            <div class="result-title">🎉 인증 토큰이 발급되었습니다!</div>
            <p style="font-size: 12px; margin-bottom: 12px; color: var(--md-sys-color-on-surface-variant);">아래 토큰을 옵시디언 플러그인 설정창의 '인증 토큰' 필드에 붙여넣어 주세요.</p>
            <div class="token-display" id="tokenDisplay"></div>
            <div class="copy-hint">대시보드로 자동 이동 중...</div>
        </div>

        <!-- 로그인이 obsidian://<action> 콜백으로 끝나는 경우 전용. 브라우저(특히 모바일)는 실제
             탭/클릭 없이 스크립트가 커스텀 스킴으로 이동시키는 걸 아무 표시 없이 막는 경우가 많아서,
             자동 리다이렉트 대신 사용자가 직접 눌러야 하는 링크로 보여준다. -->
        <div id="deviceReturnBox" class="result-box">
            <div class="result-title">✅ 로그인 완료</div>
            <p style="font-size: 12px; margin-bottom: 16px; color: var(--md-sys-color-on-surface-variant);">아래 버튼을 눌러 Obsidian으로 돌아가세요. 자동으로 넘어가지 않으면 반드시 직접 눌러야 합니다.</p>
            <a id="deviceReturnLink" class="btn" style="display: inline-block; text-decoration: none; text-align: center;" href="#">Obsidian으로 돌아가기</a>
        </div>
    </div>

    <script>
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
                themeToggleText.innerText = "다크 모드";
            } else {
                document.body.classList.remove("light-theme");
                themeToggleIcon.innerText = "☀️";
                themeToggleText.innerText = "라이트 모드";
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
        // device_token via that custom URI instead of going to /dashboard. Skip the
        // already-logged-in shortcut in that case: the visit means a device wants its own
        // fresh token, not to reuse whatever's already sitting in this browser's localStorage.
        const params = new URLSearchParams(window.location.search);
        const deviceRedirect = params.get("redirect");
        const deviceName = params.get("device_name");

        if (deviceRedirect) {
            cardSubtitle.innerText = "디바이스 로그인 승인";
        } else if (localStorage.getItem("sync_token")) {
            window.location.href = "/dashboard";
        }

        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            alertEl.style.display = "none";
            resultBox.style.display = "none";
            submitBtn.disabled = true;
            submitBtn.innerText = "로그인 중...";

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
                    alertEl.innerText = "로그인 성공! 대시보드로 이동합니다...";
                    alertEl.style.display = "block";

                    localStorage.setItem("sync_token", data.token);
                    localStorage.setItem("sync_username", data.username || data.email);
                    localStorage.setItem("sync_is_admin", data.is_admin);

                    window.location.href = "/dashboard";
                } else {
                    alertEl.className = "alert alert-error";
                    alertEl.innerText = data.error || "로그인에 실패했습니다.";
                    alertEl.style.display = "block";
                }
            } catch (err) {
                alertEl.className = "alert alert-error";
                alertEl.innerText = "서버와 통신하는 데 실패했습니다.";
                alertEl.style.display = "block";
            } finally {
                submitBtn.disabled = false;
                submitBtn.innerText = "로그인";
            }
        });
    </script>
</body>
</html>"""
    return Response(html, content_type="text/html")

@view_config(route_name='dashboard_page')
def dashboard_page_view(request):
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Obsidian Sync - Dashboard</title>
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
            <li class="nav-item active" onclick="switchTab('dashboard')">대시보드 홈</li>
            <li class="nav-item" onclick="switchTab('mypage')">내 정보 관리</li>
            <li class="nav-item" onclick="switchTab('sync')">Vault 동기화 관리</li>
            <li class="nav-item" onclick="switchTab('publish')">퍼블리시(Publish) 관리</li>
        </ul>
        <div class="user-info">
            <div class="username" id="sidebarUsername">User Name</div>
            <a class="logout" onclick="logout()">로그아웃</a>
            <div class="theme-toggle-container">
                <button class="theme-toggle-btn" id="themeToggleBtn">
                    <span id="themeToggleIcon">☀️</span> <span id="themeToggleText">라이트 모드</span>
                </button>
            </div>
        </div>
    </div>

    <div class="main-content">
        <!-- 탭 1: 대시보드 홈 -->
        <div id="tab-dashboard" class="tab-content active">
            <div class="header">
                <h1>대시보드 개요</h1>
                <p>로컬 동기화 및 퍼블리싱 상태를 모니터링합니다.</p>
            </div>
            
            <div class="widget-grid">
                <div class="card">
                    <div class="card-desc">연동된 Vault 수</div>
                    <div class="card-val" id="widgetVaultCount">0</div>
                    <div class="card-desc">동기화 완료된 저장소</div>
                </div>
                <div class="card">
                    <div class="card-desc">퍼블리시 사이트 수</div>
                    <div class="card-val" id="widgetSiteCount">0</div>
                    <div class="card-desc">배포된 문서 사이트</div>
                </div>

            </div>
        </div>

        <!-- 탭 5: 내 정보 관리 (마이페이지) -->
        <div id="tab-mypage" class="tab-content">
            <div class="header">
                <h1>내 정보 관리</h1>
                <p>개인 정보를 확인하고 수정할 수 있습니다.</p>
            </div>
            
            <div class="card" style="max-width: 600px; background: var(--md-sys-color-surface-container);">
                <div style="font-weight: 600; font-size: 16px; margin-bottom: 20px; color: var(--text-primary);">프로필 정보 변경</div>
                <div id="mypageAlert" class="alert alert-error" style="display: none; margin-bottom: 15px;"></div>
                
                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">아이디 (Username)</label>
                    <input type="text" id="mypageUsername" disabled style="width: 100%; background: rgba(0,0,0,0.1); border: 1px solid var(--md-sys-color-outline); color: var(--text-muted); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none; cursor: not-allowed;">
                </div>
                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">이름 (Name)</label>
                    <input type="text" id="mypageName" placeholder="이름을 입력하세요" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="margin-bottom: 18px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">이메일 주소 (Email)</label>
                    <input type="email" id="mypageEmail" placeholder="example@example.com" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="margin-bottom: 25px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">새 비밀번호 (변경 시에만 입력)</label>
                    <input type="password" id="mypagePassword" placeholder="••••••••" style="width: 100%; background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="margin-bottom: 25px;">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 8px; color: var(--text-primary);">나의 인증 토큰</label>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <input type="text" id="mypageToken" readonly style="flex-grow: 1; background: rgba(0,0,0,0.1); border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 13px; font-family: monospace; outline: none; height: 44px;">
                        <button class="btn-sm" onclick="copyMyPageToken()" title="토큰 복사" style="display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; padding: 0; border-radius: 50%; border: 1px solid var(--accent-blue); color: var(--accent-blue); background: transparent; cursor: pointer; transition: all 0.2s ease;">
                            <svg xmlns="http://www.w3.org/2000/svg" height="20px" viewBox="0 -960 960 960" width="20px" fill="currentColor">
                                <path d="M360-240q-33 0-56.5-23.5T280-320v-480q0-33 23.5-56.5T360-880h360q33 0 56.5 23.5T800-800v480q0-33-23.5 56.5T720-240H360Zm0-80h360v-480H360v480ZM200-80q-33 0-56.5-23.5T120-160v-560h80v560h440v80H200Zm160-240v-480 480Z"/>
                            </svg>
                        </button>
                        <button class="btn-sm" onclick="resetMyPageToken()" style="white-space: nowrap; height: 44px; border-color: var(--error-red); color: var(--error-red); background: transparent; border-radius: 100px; padding: 0 16px;">재발급</button>
                    </div>
                    <small style="display: block; font-size: 11px; color: var(--text-muted); margin-top: 6px;">
                        ※ 토큰 재발급 시 기존 클라이언트의 동기화 세션이 끊어지므로 새 토큰으로 다시 설정해야 합니다.
                    </small>
                </div>
                <div style="display: flex; justify-content: flex-end;">
                    <button class="btn" onclick="updateMyProfile()" style="width: auto;">저장하기</button>
                </div>
            </div>
        </div>

        <!-- 탭 2: Vault 동기화 관리 -->
        <div id="tab-sync" class="tab-content">
            <div class="header">
                <h1>Vault 동기화 파일 내역</h1>
                <p>각 Vault 별 동기화된 메타데이터 및 삭제 Tombstone 현황을 조회합니다.</p>
            </div>
            
            <div class="selector-bar">
                <select id="vaultSelect" onchange="loadVaultFiles()">
                    <option value="">Vault 선택...</option>
                </select>
            </div>

            <div class="table-container">
                <table id="filesTable">
                    <thead>
                        <tr>
                            <th>파일 경로</th>
                            <th>크기 (Bytes)</th>
                            <th>최종 수정 일자</th>
                            <th>해시값 (SHA-256)</th>
                            <th>상태</th>
                        </tr>
                    </thead>
                    <tbody id="filesTableBody">
                        <tr>
                            <td colspan="5" class="empty-state">Vault를 먼저 선택해 주세요.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 탭 3: 퍼블리시 관리 -->
        <div id="tab-publish" class="tab-content">
            <div class="header">
                <h1>퍼블리시(Publish) 페이지 관리</h1>
                <p>웹에 배포된 노트를 확인하고 퍼블리시를 제어합니다.</p>
            </div>

            <div class="selector-bar">
                <select id="siteSelect" onchange="loadSiteFiles()">
                    <option value="">배포된 사이트 선택...</option>
                </select>
            </div>

            <div class="table-container">
                <table id="publishTable">
                    <thead>
                        <tr>
                            <th>노트 경로</th>
                            <th>크기 (Bytes)</th>
                            <th>최종 배포 일자</th>
                            <th>공개 링크</th>
                            <th>관리</th>
                        </tr>
                    </thead>
                    <tbody id="publishTableBody">
                        <tr>
                            <td colspan="5" class="empty-state">사이트를 먼저 선택해 주세요.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 탭 4: 사용자 관리 (관리자 전용) -->
        <div id="tab-users" class="tab-content">
            <div class="header">
                <h1>사용자 계정 관리</h1>
                <p>시스템에 등록된 사용자 목록을 조회하고 제어합니다. (관리자 권한)</p>
            </div>

            <!-- 신규 사용자 추가 폼 -->
            <div class="card" style="margin-bottom: 25px; background: var(--md-sys-color-surface-container);">
                <div style="font-weight: 600; font-size: 15px; margin-bottom: 15px; color: var(--text-primary);">신규 사용자 생성</div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 15px;">
                    <input type="text" id="newUsername" placeholder="아이디 (Username)" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                    <input type="password" id="newPassword" placeholder="비밀번호" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                    <input type="text" id="newName" placeholder="이름 (Name)" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                    <input type="email" id="newEmail" placeholder="이메일 주소 (Email)" style="background: transparent; border: 1px solid var(--md-sys-color-outline); color: var(--text-primary); padding: 12px 16px; border-radius: 12px; font-size: 14px; outline: none;">
                </div>
                <div style="display: flex; justify-content: flex-end;">
                    <button class="btn" onclick="createUser()" style="width: auto;">계정 생성</button>
                </div>
            </div>
            
            <div class="table-container">
                <table id="usersTable">
                    <thead>
                        <tr>
                            <th>아이디 (ID)</th>
                            <th>이름 (Name)</th>
                            <th>이메일 주소</th>
                            <th>관리</th>
                        </tr>
                    </thead>
                    <tbody id="usersTableBody">
                        <tr>
                            <td colspan="4" class="empty-state">로딩 중...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <div id="usersPagination" class="pagination-bar"></div>
        </div>
    </div>

    <script>
        const authToken = localStorage.getItem("sync_token");
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
                themeToggleText.innerText = "다크 모드";
            } else {
                document.body.classList.remove("light-theme");
                themeToggleIcon.innerText = "☀️";
                themeToggleText.innerText = "라이트 모드";
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
        
        if (!authToken) {
            window.location.href = "/login";
        }
        
        document.getElementById("sidebarUsername").innerText = username || "Obsidian User";

        // 관리자인 경우 사이드바에 사용자 관리 메뉴 추가
        if (isAdmin) {
            const menu = document.getElementById("sidebarMenu");
            const li = document.createElement("li");
            li.className = "nav-item";
            li.setAttribute("onclick", "switchTab('users')");
            li.innerText = "사용자 관리";
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
                await fetch("/user/logout", {
                    method: "POST",
                    headers: {
                        "Authorization": "Bearer " + authToken
                    }
                });
            } catch (err) {
                console.error("Logout API failed:", err);
            }
            localStorage.removeItem("sync_token");
            localStorage.removeItem("sync_username");
            localStorage.removeItem("sync_is_admin");
            window.location.href = "/login";
        }

        async function fetchAPI(url, options = {}) {
            options.headers = options.headers || {};
            options.headers["Authorization"] = "Bearer " + authToken;
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
                    document.getElementById("mypageToken").value = data.token || "토큰 없음";
                } else {
                    alertEl.className = "alert alert-error";
                    alertEl.innerText = (data && data.error) ? data.error : "프로필을 불러오지 못했습니다.";
                    alertEl.style.display = "block";
                }
            } catch (err) {
                console.error("loadMyProfile error:", err);
                alertEl.className = "alert alert-error";
                alertEl.innerText = "프로필 정보 로드 중 오류가 발생했습니다: " + err.message;
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

        function copyMyPageToken() {
            const tokenInput = document.getElementById("mypageToken");
            if (tokenInput.value && tokenInput.value !== "토큰 없음") {
                navigator.clipboard.writeText(tokenInput.value);
                showToast("토큰이 클립보드에 복사되었습니다.");
            } else {
                showToast("복사할 토큰이 없습니다.");
            }
        }

        async function resetMyPageToken() {
            const confirmed = await showM3Confirm(
                "인증 토큰 재발급",
                `정말로 인증 토큰을 재발급하시겠습니까?\n재발급 시 기존 클라이언트의 동기화 세션이 만료되어 새로운 토큰으로 다시 설정해야 합니다.`,
                "재발급",
                true
            );
            if (!confirmed) return;
            
            const alertEl = document.getElementById("mypageAlert");
            alertEl.style.display = "none";
            
            const res = await fetchAPI("/api/user/profile/reset-token", {
                method: "POST",
                headers: { "Content-Type": "application/json" }
            });
            
            if (res && res.token) {
                alertEl.className = "alert alert-success";
                alertEl.innerText = "토큰이 성공적으로 재발급되었습니다.";
                alertEl.style.display = "block";
                
                document.getElementById("mypageToken").value = res.token;
                localStorage.setItem("sync_token", res.token);
                // 세션 유지를 위해 페이지를 부드럽게 새로고침합니다.
                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            } else {
                alertEl.className = "alert alert-error";
                alertEl.innerText = res ? res.error : "토큰 재발급에 실패했습니다.";
                alertEl.style.display = "block";
            }
        }

        async function updateMyProfile() {
            const alertEl = document.getElementById("mypageAlert");
            alertEl.style.display = "none";
            
            const name = document.getElementById("mypageName").value.trim();
            const email = document.getElementById("mypageEmail").value.trim();
            const password = document.getElementById("mypagePassword").value;
            
            if (!name || !email) {
                alertEl.className = "alert alert-error";
                alertEl.innerText = "이름과 이메일은 필수 입력 사항입니다.";
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
                alertEl.innerText = "정보가 성공적으로 수정되었습니다.";
                alertEl.style.display = "block";
                
                // 로컬 정보 갱신 및 UI 업데이트
                localStorage.setItem("sync_username", name);
                document.getElementById("sidebarUsername").innerText = name;
                document.getElementById("mypagePassword").value = "";
            } else {
                alertEl.className = "alert alert-error";
                alertEl.innerText = res ? res.error : "수정에 실패했습니다.";
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
            select.innerHTML = '<option value="">Vault 선택...</option>';
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
                tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Vault를 먼저 선택해 주세요.</td></tr>';
                return;
            }
            tbody.innerHTML = '<tr><td colspan="5" class="empty-state">로딩 중...</td></tr>';

            const data = await fetchAPI("/api/admin/vaults/" + vaultId);
            tbody.innerHTML = "";
            
            if (data && data.files && data.files.length > 0) {
                data.files.forEach(f => {
                    const tr = document.createElement("tr");
                    
                    const timeStr = new Date(f.modified_at_ms).toLocaleString();
                    const stateBadge = f.is_deleted 
                        ? '<span class="badge badge-deleted">삭제됨 (Tombstone)</span>' 
                        : '<span class="badge badge-active">동기화 활성</span>';
                    
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
                tbody.innerHTML = '<tr><td colspan="5" class="empty-state">동기화된 파일이 없습니다.</td></tr>';
            }
        }

        async function refreshSiteList() {
            const data = await fetchAPI("/api/admin/published");
            const select = document.getElementById("siteSelect");
            select.innerHTML = '<option value="">배포된 사이트 선택...</option>';
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
                tbody.innerHTML = '<tr><td colspan="5" class="empty-state">사이트를 먼저 선택해 주세요.</td></tr>';
                return;
            }
            tbody.innerHTML = '<tr><td colspan="5" class="empty-state">로딩 중...</td></tr>';

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
                        <td><a href="${pubLink}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">링크 열기 ↗</a></td>
                        <td><button class="btn-sm" onclick="unpublishPage('${siteId}', '${f.path}')">퍼블리시 해제</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-state">게시된 페이지가 없습니다.</td></tr>';
            }
        }

        async function unpublishPage(siteId, path) {
            const confirmed = await showM3Confirm(
                "퍼블리시 해제",
                `'${path}' 파일을 정말로 퍼블리시 해제하시겠습니까?`,
                "해제",
                true,
                "warning"
            );
            if (!confirmed) return;
            
            const res = await fetchAPI("/api/remove", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: siteId, path: path, token: authToken })
            });

            if (res && res.ok) {
                showToast("성공적으로 제거되었습니다.");
                loadSiteFiles();
            } else {
                showToast("제거 실패: " + (res ? res.error : "알 수 없는 오류"));
            }
        }

        // 사용자 목록 로드 (관리자 전용)
        async function loadUsersList(page = 1) {
            currentUsersPage = page;
            const tbody = document.getElementById("usersTableBody");
            const paginationDiv = document.getElementById("usersPagination");
            tbody.innerHTML = '<tr><td colspan="4" class="empty-state">로딩 중...</td></tr>';
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
                        ? '<span class="badge badge-active">본인 계정</span>' 
                        : `<button class="btn-sm" onclick="deleteUser('${u.username}')" style="margin-right: 8px;">삭제</button>`;
                    
                    const resetPwBtnHtml = `<button class="btn-sm" onclick="resetUserPassword('${u.username}')" style="border-color: var(--accent-blue); color: var(--accent-blue); background: transparent; margin-right: 8px;">비밀번호 변경</button>`;
                    const resetTokenBtnHtml = `<button class="btn-sm" onclick="resetUserToken('${u.username}')" style="border-color: var(--accent-purple); color: var(--accent-purple); background: transparent;">토큰 재발급</button>`;
                    
                    tr.innerHTML = `
                        <td>${u.username}</td>
                        <td>${u.name || '-'}</td>
                        <td>${u.email || '-'}</td>
                        <td>
                            ${deleteBtnHtml}
                            ${resetPwBtnHtml}
                            ${resetTokenBtnHtml}
                        </td>
                    `;
                    tbody.appendChild(tr);
                });

                if (data.total && data.total > limit) {
                    const totalPages = Math.ceil(data.total / limit);
                    renderPagination(totalPages, page);
                }
            } else {
                tbody.innerHTML = '<tr><td colspan="4" class="empty-state">사용자가 존재하지 않습니다.</td></tr>';
            }
        }

        function renderPagination(totalPages, currentPage) {
            const paginationDiv = document.getElementById("usersPagination");
            if (!paginationDiv) return;
            paginationDiv.innerHTML = "";

            // 이전 버튼
            const prevBtn = document.createElement("button");
            prevBtn.className = "pagination-btn";
            prevBtn.innerText = "이전";
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
            nextBtn.innerText = "다음";
            nextBtn.disabled = currentPage === totalPages;
            nextBtn.onclick = () => loadUsersList(currentPage + 1);
            paginationDiv.appendChild(nextBtn);
        }

        function showM3Confirm(title, content, confirmText = "삭제", isDestructive = true) {
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

        function showM3Prompt(title, content, placeholder = "입력하세요") {
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
                confirmBtn.innerText = "변경";
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
                "사용자 계정 삭제",
                `정말로 사용자 '${targetUsername}'을(를) 삭제하시겠습니까?\n이 작업은 되돌릴 수 없으며 소유한 Vault 및 동기화 이력 소유권이 영구 소멸됩니다.`,
                "삭제",
                true
            );
            if (!confirmed) return;
            
            const res = await fetchAPI("/api/admin/users/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: targetUsername })
            });
            
            if (res && res.ok) {
                showToast("사용자가 성공적으로 삭제되었습니다.");
                loadUsersList(currentUsersPage);
            } else {
                showToast("삭제 실패: " + (res ? res.error : "알 수 없는 오류"));
            }
        }

        async function resetUserPassword(targetUsername) {
            const newPassword = await showM3Prompt(
                "비밀번호 변경",
                `사용자 '${targetUsername}'의 새 비밀번호를 입력하세요:`,
                "새 비밀번호 입력"
            );
            if (newPassword === null) return; // 취소
            if (!newPassword.trim()) {
                showToast("비밀번호는 공백일 수 없습니다.");
                return;
            }
            
            const res = await fetchAPI("/api/admin/users/reset-password", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: targetUsername, password: newPassword })
            });
            
            if (res && res.ok) {
                showToast("비밀번호가 성공적으로 변경되었습니다.");
            } else {
                showToast("비밀번호 변경 실패: " + (res ? res.error : "알 수 없는 오류"));
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
                showToast("아이디와 비밀번호를 모두 입력해 주세요.");
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
                showToast(`사용자 '${newUsername}'이(가) 성공적으로 생성되었습니다.`);
                usernameInput.value = "";
                passwordInput.value = "";
                nameInput.value = "";
                emailInput.value = "";
                loadUsersList(1);
            } else {
                showToast("사용자 생성 실패: " + (res ? res.error : "알 수 없는 오류"));
            }
        }

        async function resetUserToken(targetUsername) {
            const confirmed = await showM3Confirm(
                "인증 토큰 강제 재발급",
                `사용자 '${targetUsername}'의 인증 토큰을 정말로 강제 재발급하시겠습니까?\n재발급 시 기존 클라이언트의 동기화 세션이 만료되어 새 토큰으로 다시 설정해야 합니다.`,
                "재발급",
                true
            );
            if (!confirmed) return;
            
            const res = await fetchAPI("/api/admin/users/reset-token", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: targetUsername })
            });
            
            if (res && res.ok) {
                showToast(`토큰이 성공적으로 재발급되었습니다.\n새 토큰: ${res.token}`);
                loadUsersList(currentUsersPage);
            } else {
                showToast("토큰 재발급 실패: " + (res ? res.error : "알 수 없는 오류"));
            }
        }

        // 초기 로드
        loadDashboardStats();
    </script>
    <div id="m3DialogOverlay" class="dialog-overlay" style="display: none;">
        <div class="dialog-card">
            <h2 class="dialog-title" id="dialogTitle">삭제 확인</h2>
            <div class="dialog-content" id="dialogContent">정말로 삭제하시겠습니까?</div>
            <div id="dialogInputContainer" class="dialog-input-container" style="display: none;">
                <input type="password" id="dialogInput" class="dialog-input" placeholder="새 비밀번호 입력">
            </div>
            <div class="dialog-actions">
                <button class="dialog-btn dialog-btn-text" id="dialogCancelBtn">취소</button>
                <button class="dialog-btn dialog-btn-filled" id="dialogConfirmBtn">확인</button>
            </div>
        </div>
    </div>
    <div id="toastContainer" class="toast-container"></div>
</body>
</html>"""
    return Response(html, content_type="text/html")

@view_config(route_name='admin_vaults', renderer='json')
def admin_vaults_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")

    # No admin bypass, intentionally: even the existence/name of another user's vault isn't
    # admin's to see, only their own.
    vaults = request.repository.get_vaults_by_owner(user_info["username"])
    return {"vaults": vaults}

@view_config(route_name='admin_vault_files', renderer='json')
def admin_vault_files_view(request):
    vault_id = request.matchdict.get("vault_id")
    auth_res = verify_vault_ownership(request, vault_id)
    if auth_res is None:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if auth_res is False:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

    files_meta = request.repository.load_all(vault_id)
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

@view_config(route_name='admin_published', renderer='json')
def admin_published_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")

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
                        # nothing extra being exposed by this listing.
                        if user_info["is_admin"]:
                            sites.append({"id": item, "username": user_dir})
                        else:
                            owner = request.repository.get_vault_owner(item)
                            if user_dir == user_info["username"] or owner == user_info["username"]:
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

@view_config(route_name='user_login', renderer='json')
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
                # "token" is this account's web-dashboard token (users.token); "device_token" is a
                # freshly minted, independent gRPC sync credential for whichever client just logged
                # in -- logging in again from a different device doesn't invalidate earlier ones.
                admin_row = request.repository.get_user_by_username(admin_user)
                web_token = admin_row["token"] if admin_row else None
                if not web_token:
                    web_token = secrets.token_hex(32)
                    request.repository.update_user_token(admin_user, web_token)
                device_token = secrets.token_hex(32)
                request.repository.create_device_token(device_token, admin_user, device_name, int(time.time() * 1000))
                logger.info(f"Successful admin login for user '{username}' via environment variables.")
                return {
                    "token": web_token,
                    "device_token": device_token,
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

        token = user["token"]
        # Regenerate and assign a new token if it's empty
        if not token:
            token = secrets.token_hex(32)
            request.repository.update_user_token(username, token)

        logger.info(f"Successful login for user '{username}'.")

        # "token" above is this account's web-dashboard token (users.token); "device_token" is a
        # freshly minted, independent gRPC sync credential for whichever client just logged in --
        # logging in again from a different device doesn't invalidate earlier ones.
        device_token = secrets.token_hex(32)
        request.repository.create_device_token(device_token, username, device_name, int(time.time() * 1000))

        return {
            "token": token,
            "device_token": device_token,
            "username": user.get("username"),
            "email": user.get("email") or user.get("username"),
            "name": user.get("name") or user.get("username").split("@")[0],
            "is_admin": bool(user.get("is_admin", False))
        }
    except Exception as e:
        logger.error(f"User login process failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='get_token_info', renderer='json')
def get_token_info(request):
    # This route isn't in token_auth_tween's bypass list, so reaching this point means the token is
    # already valid — when the client builds the publish site URL (/publish/{username}/{vault}/...),
    # it needs to use the username the server actually recognizes, not the userName setting (a
    # free-text display label the user can type anything into), or the path won't line up. With the
    # master admin token, this resolves to the ADMIN_USER env var (or "admin" if unset), which can
    # differ from the client's display name.
    user_info = get_authenticated_user(request)
    return {
        "authorized": user_info is not None,
        "username": user_info["username"] if user_info else None,
        "database_connected": request.repository is not None,
        "server_time": int(time.time() * 1000)
    }

# 1. API for looking up a file's backup version history
@view_config(route_name='get_history', renderer='json')
def get_history_view(request):
    vault_id = request.params.get("vault_id")
    path = request.params.get("path")
    if not vault_id or not path:
        return Response(body=json.dumps({"error": "Missing vault_id or path"}).encode("utf-8"), status=400, content_type="application/json")
        
    auth_res = verify_vault_ownership(request, vault_id)
    if auth_res is None:
        return Response(body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), status=401, content_type="application/json")
    if auth_res is False:
        return Response(body=json.dumps({"error": "Forbidden"}).encode("utf-8"), status=403, content_type="application/json")

    try:
        history_rows = request.repository.get_history(vault_id, path)
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
@view_config(route_name='download_history')
def download_history_view(request):
    vault_id = request.params.get("vault_id")
    history_id_str = request.params.get("history_id")
    if not vault_id or not history_id_str:
        return Response("Missing vault_id or history_id", status=400)
        
    auth_res = verify_vault_ownership(request, vault_id)
    if auth_res is None:
        return Response("Unauthorized", status=401)
    if auth_res is False:
        return Response("Forbidden", status=403)

    try:
        history_id = int(history_id_str)
        row = request.repository.get_history_by_id(history_id)
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
@view_config(route_name='restore_history', renderer='json')
def restore_history_view(request):
    try:
        body = request.json_body
        vault_id = body.get("vault_id")
        history_id = body.get("history_id")
        req_path = body.get("path", "")
        
        if not vault_id or history_id is None:
            return Response(body=json.dumps({"error": "Missing vault_id or history_id"}).encode("utf-8"), status=400, content_type="application/json")
            
        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        row = request.repository.get_history_by_id(history_id)
        if not row:
            return {"ok": False, "error": "History version not found"}
            
        backup_file_path = row["backup_file_path"]
        if not os.path.exists(backup_file_path):
            return {"ok": False, "error": "Backup file not found on disk"}
            
        target_path = req_path if req_path else row["path"]
        
        data_dir = request.registry.settings.get("data_dir")
        vault_path = get_vault_path(data_dir, vault_id)
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
        request.repository.save_one(vault_id, target_path, meta)
        
        try:
            device_name = unquote(request.headers.get("X-Device-Name", "Unknown Device"))
            user_name = unquote(request.headers.get("X-User-Name", "Unknown User"))
            
            request.repository.add_history(
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

@view_config(route_name='publish_list', renderer='json')
def publish_list_view(request):
    try:
        vault_id = None
        try:
            body = request.json_body
            vault_id = body.get("id") or body.get("obs-id")
        except Exception:
            pass

        if not vault_id:
            vault_id = request.headers.get("obs-id")

        if not vault_id:
            return Response(body=json.dumps({"error": "Missing obs-id header"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), status=401, content_type="application/json")
        if auth_res is False:
            return Response(body=json.dumps({"error": "Forbidden"}).encode("utf-8"), status=403, content_type="application/json")

        files = request.repository.get_published_files(vault_id)

        data_dir = request.registry.settings.get("data_dir")
        published_base = os.path.join(data_dir, "published", vault_id)

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

@view_config(route_name='publish_upload', renderer='json')
def publish_upload_view(request):
    try:
        vault_id = request.headers.get("obs-id")
        obs_path_enc = request.headers.get("obs-path")
        obs_hash = request.headers.get("obs-hash")
        
        if not vault_id or not obs_path_enc or not obs_hash:
            return Response(body=json.dumps({"error": "Missing obs-id, obs-path or obs-hash header"}).encode("utf-8"), status=400, content_type="application/json")
            
        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), status=401, content_type="application/json")
        if auth_res is False:
            return Response(body=json.dumps({"error": "Forbidden"}).encode("utf-8"), status=403, content_type="application/json")

        path = unquote(obs_path_enc)
        
        # File storage location: data_dir/published/username/vault_id/path
        data_dir = request.registry.settings.get("data_dir")
        username = auth_res["username"]
        published_base = os.path.abspath(os.path.join(data_dir, "published", username, vault_id))
        dest_file_path = os.path.abspath(os.path.join(published_base, path))
        
        if not dest_file_path.startswith(published_base):
            return Response(body=json.dumps({"error": "Path traversal detected"}).encode("utf-8"), status=400, content_type="application/json")
            
        os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
        
        body_data = request.body
        with open(dest_file_path, "wb") as f:
            f.write(body_data)
            
        request.repository.add_published_file(vault_id, path, obs_hash)
        logger.info(f"Published file saved: vault={vault_id}, path={path}, hash={obs_hash}")
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_upload failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_remove', renderer='json')
def publish_remove_view(request):
    try:
        # Obsidian Publish format: JSON body {path, id, token}
        body = request.json_body
        path = body.get("path")
        vault_id = body.get("id") or request.headers.get("obs-id")
        if not vault_id:
            return Response(body=json.dumps({"error": "Missing id in body"}).encode("utf-8"), status=400, content_type="application/json")
        if not path:
            return Response(body=json.dumps({"error": "Missing path in body"}).encode("utf-8"), status=400, content_type="application/json")
            
        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), status=401, content_type="application/json")
        if auth_res is False:
            return Response(body=json.dumps({"error": "Forbidden"}).encode("utf-8"), status=403, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        username = auth_res["username"]
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
                
        request.repository.remove_published_file(vault_id, path)
        logger.info(f"Published file removed: vault={vault_id}, path={path}")
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_remove failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_download')
def publish_download_view(request):
    try:
        body = request.json_body
        vault_id = body.get("id")
        file_path = body.get("path")
        if not vault_id or not file_path:
            return Response("Missing id or path", status=400)

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response("Unauthorized", status=401)
        if auth_res is False:
            return Response("Forbidden", status=403)

        data_dir = request.registry.settings.get("data_dir")
        username = auth_res["username"]
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

@view_config(route_name='publish_view')
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


@view_config(route_name='publish_slugs', renderer='json')
def publish_slugs_view(request):
    try:
        body = request.json_body
        ids = body.get("ids", [])
        data_dir = request.registry.settings.get("data_dir")
        result = {}
        for vault_id in ids:
            auth_res = verify_vault_ownership(request, vault_id)
            if not auth_res:
                continue
            site = load_publish_meta(data_dir, vault_id, "site.json", {})
            result[vault_id] = site.get("slug", vault_id)
        return result
    except Exception as e:
        logger.error(f"HTTP publish_slugs failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_site', renderer='json')
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
        if os.path.isdir(meta_root):
            for vault_id in os.listdir(meta_root):
                vault_meta_dir = os.path.join(meta_root, vault_id)
                if not os.path.isdir(vault_meta_dir):
                    continue
                site = load_publish_meta(data_dir, vault_id, "site.json", {})
                if site.get("slug") == slug:
                    return {"id": vault_id, "slug": slug, "host": site.get("host", "")}

        return Response(body=json.dumps({"error": "Not found"}).encode("utf-8"), status=404, content_type="application/json")
    except Exception as e:
        logger.error(f"HTTP publish_site failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_customurl', renderer='json')
def publish_customurl_view(request):
    return {"url": "", "redirect": False}

@view_config(route_name='publish_slug', renderer='json')
def publish_slug_view(request):
    try:
        body = request.json_body
        vault_id = body.get("id")
        host = body.get("host", "")
        slug = body.get("slug", "")
        if not vault_id:
            return Response(body=json.dumps({"error": "Missing id"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        site = load_publish_meta(data_dir, vault_id, "site.json", {})
        site["slug"] = slug
        site["host"] = host
        save_publish_meta(data_dir, vault_id, "site.json", site)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_slug failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_password', renderer='json')
def publish_password_view(request):
    try:
        body = request.json_body
        vault_id = body.get("id")
        if not vault_id:
            return Response(body=json.dumps({"error": "Missing id"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        passwords = load_publish_meta(data_dir, vault_id, "passwords.json", [])

        name = body.get("name")
        pw = body.get("pw")
        del_name = body.get("del")

        if del_name:
            passwords = [p for p in passwords if p.get("name") != del_name]
            save_publish_meta(data_dir, vault_id, "passwords.json", passwords)
            return {"ok": True}
        elif name and pw:
            passwords = [p for p in passwords if p.get("name") != name]
            passwords.append({"name": name, "pw": pw})
            save_publish_meta(data_dir, vault_id, "passwords.json", passwords)
            return {"ok": True}
        else:
            # GET: return list without pw values
            return {"pass": [{"name": p["name"]} for p in passwords]}
    except Exception as e:
        logger.error(f"HTTP publish_password failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")


@view_config(route_name='publish_share_list', renderer='json')
def publish_share_list_view(request):
    try:
        body = request.json_body
        vault_id = body.get("site_uid")
        if not vault_id:
            return Response(body=json.dumps({"error": "Missing site_uid"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        shares = load_publish_meta(data_dir, vault_id, "shares.json", [])
        # Don't expose invite_code externally
        public_shares = [
            {"uid": s["uid"], "email": s["email"], "name": s.get("name", ""), "accepted": s.get("accepted", False)}
            for s in shares
        ]
        return {"shares": public_shares}
    except Exception as e:
        logger.error(f"HTTP publish_share_list failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_invite', renderer='json')
def publish_share_invite_view(request):
    try:
        body = request.json_body
        vault_id = body.get("site_uid")
        email = body.get("email")
        if not vault_id or not email:
            return Response(body=json.dumps({"error": "Missing site_uid or email"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        shares = load_publish_meta(data_dir, vault_id, "shares.json", [])
        new_share = {
            "uid": str(uuid.uuid4()),
            "email": email,
            "name": "",
            "accepted": False,
            "invite_code": str(uuid.uuid4()),
        }
        shares.append(new_share)
        save_publish_meta(data_dir, vault_id, "shares.json", shares)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_share_invite failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_remove', renderer='json')
def publish_share_remove_view(request):
    try:
        body = request.json_body
        vault_id = body.get("site_uid")
        share_uid = body.get("share_uid")
        if not vault_id or not share_uid:
            return Response(body=json.dumps({"error": "Missing site_uid or share_uid"}).encode("utf-8"), status=400, content_type="application/json")

        auth_res = verify_vault_ownership(request, vault_id)
        if auth_res is None:
            return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        if auth_res is False:
            return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        shares = load_publish_meta(data_dir, vault_id, "shares.json", [])
        shares = [s for s in shares if s.get("uid") != share_uid]
        save_publish_meta(data_dir, vault_id, "shares.json", shares)
        return {"ok": True}
    except Exception as e:
        logger.error(f"HTTP publish_share_remove failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='publish_share_accept', renderer='json')
def publish_share_accept_view(request):
    try:
        body = request.json_body
        code = body.get("code")
        if not code:
            return Response(body=json.dumps({"error": "Missing code"}).encode("utf-8"), status=400, content_type="application/json")

        data_dir = request.registry.settings.get("data_dir")
        meta_root = os.path.join(data_dir, "publish_meta")
        if os.path.isdir(meta_root):
            for vault_id in os.listdir(meta_root):
                vault_meta_dir = os.path.join(meta_root, vault_id)
                if not os.path.isdir(vault_meta_dir):
                    continue
                shares = load_publish_meta(data_dir, vault_id, "shares.json", [])
                changed = False
                for s in shares:
                    if s.get("invite_code") == code:
                        s["accepted"] = True
                        changed = True
                if changed:
                    save_publish_meta(data_dir, vault_id, "shares.json", shares)
                    return {"ok": True}

        return Response(body=json.dumps({"error": "Invalid code"}).encode("utf-8"), status=404, content_type="application/json")
    except Exception as e:
        logger.error(f"HTTP publish_share_accept failed: {e}")
        return Response(body=json.dumps({"error": str(e)}).encode("utf-8"), status=500, content_type="application/json")

@view_config(route_name='user_logout', renderer='json')
def user_logout_view(request):
    user_info = get_authenticated_user(request)
    if user_info and not user_info["is_admin"]:
        request.repository.update_user_token(user_info["username"], None)
        logger.info(f"User '{user_info['username']}' logged out, token cleared.")
    return {"ok": True}

@view_config(route_name='admin_users', renderer='json')
def admin_users_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if not user_info["is_admin"]:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")
        
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

@view_config(route_name='admin_user_delete', renderer='json')
def admin_user_delete_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if not user_info["is_admin"]:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")
        
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

@view_config(route_name='admin_user_reset_password', renderer='json')
def admin_user_reset_password_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if not user_info["is_admin"]:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")
        
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

@view_config(route_name='admin_user_create', renderer='json')
def admin_user_create_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if not user_info["is_admin"]:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")
        
    try:
        body = request.json_body
        username = body.get("username")
        password = body.get("password")
        name = body.get("name")
        email = body.get("email")
        if not username or not password:
            return Response(status=400, body=json.dumps({"error": "Missing username or password"}).encode("utf-8"), content_type="application/json")
            
        pw_hash = hash_password(password)
        token = secrets.token_hex(32)
        success = request.repository.create_user(
            username=username,
            password_hash=pw_hash,
            token=token,
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

@view_config(route_name='admin_user_reset_token', renderer='json')
def admin_user_reset_token_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
    if not user_info["is_admin"]:
        return Response(status=403, body=json.dumps({"error": "Forbidden"}).encode("utf-8"), content_type="application/json")
        
    try:
        body = request.json_body
        username = body.get("username")
        if not username:
            return Response(status=400, body=json.dumps({"error": "Missing username"}).encode("utf-8"), content_type="application/json")
            
        new_token = secrets.token_hex(32)
        request.repository.update_user_token(username, new_token)
        logger.info(f"Admin '{user_info['username']}' reset token for user '{username}'")
        return {"ok": True, "token": new_token}
    except Exception as e:
        logger.error(f"Failed to reset token: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='user_profile', renderer='json')
def user_profile_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        
    username = user_info["username"]

    # Look up the user in the DB (the env-var admin account has a real DB row too, created at
    # startup -- see main.py)
    user = request.repository.get_user_by_username(username)
    if not user:
        return Response(status=404, body=json.dumps({"error": "User not found"}).encode("utf-8"), content_type="application/json")
        
    return {
        "username": user["username"],
        "name": user.get("name") or "",
        "email": user.get("email") or "",
        "token": user.get("token") or "",
        "is_admin": bool(user.get("is_admin", False))
    }

@view_config(route_name='user_profile_reset_token', renderer='json')
def user_profile_reset_token_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        
    username = user_info["username"]
    
    # Block the env-var admin from reissuing the master token
    admin_user = os.getenv("ADMIN_USER") or "admin"
    if username == admin_user:
        return Response(status=400, body=json.dumps({"error": "Cannot reset default system administrator token from the UI."}).encode("utf-8"), content_type="application/json")
        
    try:
        new_token = secrets.token_hex(32)
        request.repository.update_user_token(username, new_token)
        logger.info(f"User '{username}' reset their own API token.")
        return {"ok": True, "token": new_token}
    except Exception as e:
        logger.error(f"Failed to reset user's own token: {e}")
        return Response(status=500, body=json.dumps({"error": str(e)}).encode("utf-8"), content_type="application/json")

@view_config(route_name='user_profile_update', renderer='json')
def user_profile_update_view(request):
    user_info = get_authenticated_user(request)
    if not user_info:
        return Response(status=401, body=json.dumps({"error": "Unauthorized"}).encode("utf-8"), content_type="application/json")
        
    username = user_info["username"]
    
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

    # Add tweens: a tween added later runs on the outermost layer —
    # add in the order token_auth → cors so cors (CORS preflight) runs first
    config.add_tween("server.web.token_auth_tween_factory")
    config.add_tween("server.web.cors_tween_factory")

    # Add routes
    config.add_route('ping', '/api/ping')
    config.add_route('login_page', '/login')
    config.add_route('dashboard_page', '/dashboard')
    config.add_route('user_login', '/user/login')
    config.add_route('user_logout', '/user/logout')
    config.add_route('user_profile', '/api/user/profile')
    config.add_route('user_profile_update', '/api/user/profile/update')
    config.add_route('user_profile_reset_token', '/api/user/profile/reset-token')
    config.add_route('admin_users', '/api/admin/users')
    config.add_route('admin_user_create', '/api/admin/users/create')
    config.add_route('admin_user_reset_token', '/api/admin/users/reset-token')
    config.add_route('admin_user_delete', '/api/admin/users/delete')
    config.add_route('admin_user_reset_password', '/api/admin/users/reset-password')
    config.add_route('admin_vaults', '/api/admin/vaults')
    config.add_route('admin_vault_files', '/api/admin/vaults/{vault_id}')
    config.add_route('admin_published', '/api/admin/published')
    config.add_route('get_token_info', '/api/token/info')
    config.add_route('get_history', '/api/history')
    config.add_route('download_history', '/api/history/download')
    config.add_route('restore_history', '/api/history/restore')
    config.add_route('publish_list', '/api/list')
    config.add_route('publish_upload', '/api/upload')
    config.add_route('publish_remove', '/api/remove')
    config.add_route('publish_download', '/api/download')
    config.add_route('publish_slugs', '/api/slugs')
    config.add_route('publish_site', '/api/site')
    config.add_route('publish_customurl', '/api/customurl')
    config.add_route('publish_slug', '/api/slug')
    config.add_route('publish_password', '/api/password')
    # Register the specific /publish/share/* routes before the wildcard /publish/{vault_id}/{path:.*}
    config.add_route('publish_share_list', '/publish/share/list')
    config.add_route('publish_share_invite', '/publish/share/invite')
    config.add_route('publish_share_remove', '/publish/share/remove')
    config.add_route('publish_share_accept', '/publish/share/accept')
    config.add_route('publish_view', '/publish/{username}/{vault_id}/{path:.*}')
    
    # Scan for decorators (@view_config, etc.)
    config.scan(__name__)
    
    logger.info("Pyramid Web Application successfully created and configured.")
    return config.make_wsgi_app()
