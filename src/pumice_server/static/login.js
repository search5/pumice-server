// Depends on a global `I18N` object defined by an inline <script> in login.jinja2 before this
// file is loaded (the fully-resolved translation blob for the request's negotiated language).
function t(key, params) {
    let node = I18N;
    for (const part of key.split(".")) {
        if (node == null) break;
        node = node[part];
    }
    let text = (typeof node === "string") ? node : key;
    if (params) {
        text = text.replace(/\{\{\s*([\w-]+)\s*\}\}/g, (_m, name) => (params[name] !== undefined ? String(params[name]) : ""));
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
