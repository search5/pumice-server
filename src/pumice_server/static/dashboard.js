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

            const pluginsLi = document.createElement("li");
            pluginsLi.className = "nav-item";
            pluginsLi.setAttribute("onclick", "switchTab('plugins')");
            pluginsLi.innerText = t("plugins.title");
            menu.appendChild(pluginsLi);
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
                // 방금 생성한 디바이스 토큰은 1회성 노출이라, 마이페이지에 새로 들어올 때마다
                // 이전에 봤던(어쩌면 훨씬 전에 생성된) 토큰이 계속 남아있으면 안 된다.
                document.getElementById("newDeviceTokenBox").style.display = "none";
                loadMyProfile();
                loadMyDevices();
            } else if (tabId === 'sync') {
                refreshVaultList();
            } else if (tabId === 'publish') {
                refreshSiteList();
            } else if (tabId === 'users') {
                loadUsersList(1);
            } else if (tabId === 'plugins') {
                loadPluginsTab();
            }
        }

        function switchMypageSubtab(subtabId) {
            document.querySelectorAll(".subtab-item").forEach(item => item.classList.remove("active"));
            document.querySelectorAll(".subtab-content").forEach(el => el.classList.remove("active"));
            // 같은 이유로 -- 디바이스 하위 탭을 벗어났다 돌아와도 지난 토큰이 다시 보이면 안 된다.
            document.getElementById("newDeviceTokenBox").style.display = "none";

            const clickedItem = Array.from(document.querySelectorAll(".subtab-item")).find(item => {
                const attr = item.getAttribute("onclick");
                return attr && attr.includes(subtabId);
            });
            if (clickedItem) clickedItem.classList.add("active");

            document.getElementById("mypage-subtab-" + subtabId).classList.add("active");
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
                contentEl.innerHTML = content.replace(new RegExp("\\n", "g"), "<br>").replace(new RegExp("\n", "g"), "<br>");
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
                contentEl.innerHTML = content.replace(new RegExp("\\n", "g"), "<br>").replace(new RegExp("\n", "g"), "<br>");
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

        // 서버 플러그인 관리 (스켈레톤) -- 나열만 한다. 무엇이 설치돼있는지는 이 서버 이미지가
        // 빌드될 때 이미 정해져 있다(pyproject.toml의 일반 의존성 +
        // [project.entry-points."pumice_server.plugins"]). 관리자도 여기서 설치/삭제를 할 수
        // 없다. "활성화"는 관리자가 아니라 각 vault 소유자가 퍼블리시 탭에서 그 vault에 대해서만
        // 하는 셀프 서비스이다(아래 loadSitePlugins/toggleSitePlugin) -- 옵시디언 자체가
        // 플러그인을 vault마다 켜고 끄는 것과 같은 스코프. 이 화면의 "활성화 현황" 표는 읽기
        // 전용이다.
        async function loadPluginsTab() {
            const data = await fetchAPI("/api/admin/plugins");
            if (!data) return;

            const installedBody = document.getElementById("pluginsInstalledTableBody");
            if (data.installed && data.installed.length > 0) {
                installedBody.innerHTML = "";
                data.installed.forEach(p => {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td>${p.id}</td>
                        <td>${p.name}</td>
                        <td>${p.version}</td>
                        <td>${p.description}</td>
                    `;
                    installedBody.appendChild(tr);
                });
            } else {
                installedBody.innerHTML = `<tr><td colspan="4" class="empty-state">${t("plugins.empty_no_plugins")}</td></tr>`;
            }

            const enabledBody = document.getElementById("pluginsEnabledTableBody");
            if (data.enabled && data.enabled.length > 0) {
                enabledBody.innerHTML = "";
                data.enabled.forEach(reg => {
                    const tr = document.createElement("tr");
                    const enabledAt = reg.enabled_at_ms ? new Date(reg.enabled_at_ms).toLocaleString() : "-";
                    tr.innerHTML = `
                        <td>${reg.owner_username}</td>
                        <td>${reg.vault_id}</td>
                        <td>${reg.plugin_id}</td>
                        <td>${enabledAt}</td>
                    `;
                    enabledBody.appendChild(tr);
                });
            } else {
                enabledBody.innerHTML = `<tr><td colspan="4" class="empty-state">${t("plugins.empty_no_enabled")}</td></tr>`;
            }
        }

        // 퍼블리시 탭의 "플러그인" 표 -- 선택된 vault에 대해서만, vault 소유자가 설치된
        // 플러그인 중 켜고 끌 것을 스스로 고른다. 계정 전체 설정이 아니라 이 vault만의 설정.
        async function loadSitePlugins() {
            const select = document.getElementById("siteSelect");
            const siteId = select.value;
            const body = document.getElementById("publishPluginsTableBody");
            if (!siteId) {
                body.innerHTML = `<tr><td colspan="5" class="empty-state">${t("publish.empty_select_site")}</td></tr>`;
                return;
            }
            body.innerHTML = `<tr><td colspan="5" class="empty-state">${t("common.loading")}</td></tr>`;

            const data = await fetchAPI("/api/publish/plugins", {
                headers: { "obs-id": siteId }
            });
            if (!data) return;

            if (data.installed && data.installed.length > 0) {
                body.innerHTML = "";
                data.installed.forEach(p => {
                    const tr = document.createElement("tr");
                    const statusLabel = p.enabled ? t("myplugins.status_enabled") : t("myplugins.status_disabled");
                    const buttonLabel = p.enabled ? t("myplugins.button_disable") : t("myplugins.button_enable");
                    tr.innerHTML = `
                        <td>${p.id}</td>
                        <td>${p.description}</td>
                        <td>${p.version}</td>
                        <td>${statusLabel}</td>
                        <td><button class="btn-sm" onclick="toggleSitePlugin('${p.id}', ${p.enabled})">${buttonLabel}</button></td>
                    `;
                    body.appendChild(tr);
                });
            } else {
                body.innerHTML = `<tr><td colspan="5" class="empty-state">${t("myplugins.empty_no_plugins")}</td></tr>`;
            }
        }

        async function toggleSitePlugin(pluginId, currentlyEnabled) {
            const siteId = document.getElementById("siteSelect").value;
            if (!siteId) return;

            const url = currentlyEnabled ? "/api/publish/plugins/disable" : "/api/publish/plugins/enable";
            const res = await fetchAPI(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "obs-id": siteId },
                body: JSON.stringify({ plugin_id: pluginId })
            });

            if (res && res.ok) {
                showToast(currentlyEnabled ? t("myplugins.msg_disable_success") : t("myplugins.msg_enable_success"));
                loadSitePlugins();
            } else {
                const prefix = currentlyEnabled ? t("myplugins.msg_disable_failed_prefix") : t("myplugins.msg_enable_failed_prefix");
                showToast(prefix + (res ? res.error : t("common.unknown_error")));
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

        // Nextcloud 스타일 "앱 비밀번호" 생성 -- 로그인 없이, 이미 로그인된 세션에서 새 디바이스
        // 토큰을 미리 발급받아 다른 클라이언트(옵시디언 플러그인 등)에 붙여넣는 용도.
        async function createMyDevice() {
            const nameInput = document.getElementById("newDeviceName");
            const deviceName = nameInput.value.trim();
            if (!deviceName) {
                showToast(t("devices.msg_name_required"));
                return;
            }

            const res = await fetchAPI("/api/user/devices/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ device_name: deviceName })
            });

            if (res && res.ok) {
                document.getElementById("newDeviceTokenValue").innerText = res.device_token;
                document.getElementById("newDeviceTokenBox").style.display = "block";
                nameInput.value = "";
                loadMyDevices();
            } else {
                showToast(t("devices.msg_create_failed_prefix") + (res ? res.error : t("common.unknown_error")));
            }
        }

        async function copyNewDeviceToken() {
            const el = document.getElementById("newDeviceTokenValue");
            const value = el.innerText;
            if (!value) return;

            if (navigator.clipboard && navigator.clipboard.writeText) {
                try {
                    await navigator.clipboard.writeText(value);
                    showToast(t("common.msg_copied"));
                    return;
                } catch (err) {
                    // fall through to the manual-select fallback below
                }
            }

            // Clipboard API unavailable (e.g. a non-secure context) -- select the text so the
            // user can still copy it manually instead of the button silently doing nothing.
            const range = document.createRange();
            range.selectNodeContents(el);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            showToast(t("common.msg_copy_manual"));
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
