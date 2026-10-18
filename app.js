/* История — Mini App (чистый JS) */

const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

let currentChatId = null;
let currentTab = "messages";
let contacts = [];
let searchTimer = null;

function headers() {
    return {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": tg?.initData || "",
    };
}

async function apiGet(path) {
    const res = await fetch(path, { headers: headers() });
    if (!res.ok) {
        let msg = "Ошибка запроса";
        try { msg = (await res.json()).detail || msg; } catch {}
        throw new Error(msg);
    }
    return res.json();
}

async function apiDelete(path) {
    const res = await fetch(path, { method: "DELETE", headers: headers() });
    if (!res.ok) {
        let msg = "Ошибка запроса";
        try { msg = (await res.json()).detail || msg; } catch {}
        throw new Error(msg);
    }
    return res.json();
}

function showToast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 3000);
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, m => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[m]));
}

function formatDate(iso) {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        return d.toLocaleString("ru-RU", {
            day: "2-digit", month: "2-digit", year: "numeric",
            hour: "2-digit", minute: "2-digit"
        });
    } catch { return iso; }
}

/* ── Главный экран ─────────────────────────────────────────────────────── */
async function loadStats() {
    try {
        const data = await apiGet("/api/stats");
        document.getElementById("stat-deleted").textContent = data.deleted;
        document.getElementById("stat-media").textContent = data.media;
        document.getElementById("stat-chats").textContent = data.chats;
    } catch (e) {
        showToast(e.message);
    }
}

async function loadContacts(q = "") {
    const list = document.getElementById("contacts-list");
    list.innerHTML = '<p class="empty-state">Загрузка…</p>';
    try {
        const data = await apiGet("/api/contacts?q=" + encodeURIComponent(q));
        contacts = data.contacts || [];
        renderContacts();
    } catch (e) {
        list.innerHTML = `<p class="empty-state">${escapeHtml(e.message)}</p>`;
    }
}

function renderContacts() {
    const list = document.getElementById("contacts-list");
    if (!contacts.length) {
        list.innerHTML = '<p class="empty-state">Нет собеседников. Подключите Telegram Business и начните общение.</p>';
        return;
    }
    list.innerHTML = contacts.map(c => `
        <div class="contact-card" data-chat="${c.telegram_user_id || ""}">
            <div class="avatar">${escapeHtml((c.first_name || "?").charAt(0).toUpperCase())}</div>
            <div class="contact-info">
                <strong>${escapeHtml(c.first_name || "")} ${escapeHtml(c.last_name || "")}</strong>
                <span class="muted">${c.username ? "@" + escapeHtml(c.username) : "ID: " + (c.telegram_user_id || "—")}</span>
            </div>
        </div>
    `).join("");
    list.querySelectorAll(".contact-card").forEach(el => {
        el.addEventListener("click", () => openChat(el.dataset.chat));
    });
}

/* ── Экран собеседника ─────────────────────────────────────────────────── */
async function openChat(chatId) {
    if (!chatId) return;
    currentChatId = chatId;
    document.getElementById("main-screen").classList.add("hidden");
    document.getElementById("chat-screen").classList.remove("hidden");
    if (tg?.BackButton) {
        tg.BackButton.show();
        tg.BackButton.onClick(closeChat);
    }
    await loadChatContent();
}

function closeChat() {
    document.getElementById("chat-screen").classList.add("hidden");
    document.getElementById("main-screen").classList.remove("hidden");
    if (tg?.BackButton) tg.BackButton.hide();
    currentChatId = null;
}

async function loadChatContent() {
    const content = document.getElementById("chat-content");
    content.innerHTML = '<p class="empty-state">Загрузка…</p>';
    try {
        if (currentTab === "messages") {
            const data = await apiGet(`/api/chat/${currentChatId}/messages`);
            renderMessages(data);
        } else {
            const data = await apiGet(`/api/chat/${currentChatId}/media`);
            renderMedia(data);
        }
    } catch (e) {
        content.innerHTML = `<p class="empty-state">${escapeHtml(e.message)}</p>`;
    }
}

function renderMessages(data) {
    const msgs = data.messages || [];
    const dels = data.deletions || [];
    const versions = data.versions || [];
    const content = document.getElementById("chat-content");
    if (!msgs.length && !dels.length) {
        content.innerHTML = '<p class="empty-state">Пока нет сохранённых сообщений.</p>';
        return;
    }
    const versionsByMsg = {};
    versions.forEach(v => {
        if (!versionsByMsg[v.message_id]) versionsByMsg[v.message_id] = [];
        versionsByMsg[v.message_id].push(v);
    });

    const items = [];
    msgs.forEach(m => items.push({ type: "msg", time: m.sent_at, data: m }));
    dels.forEach(d => items.push({ type: "del", time: d.detected_at, data: d }));
    items.sort((a, b) => (b.time || "").localeCompare(a.time || ""));

    content.innerHTML = items.map(item => {
        if (item.type === "del") {
            return `
                <div class="event-card deleted">
                    <div class="event-badge">🗑 УДАЛЕНО</div>
                    <p class="muted">Обнаружено: ${formatDate(item.data.detected_at)}</p>
                    <p>ID сообщений: ${item.data.message_ids.join(", ")}</p>
                </div>
            `;
        }
        const m = item.data;
        const edits = versionsByMsg[m.id] || [];
        const editsHtml = edits.map(v => `
            <div class="edit-block">
                <div class="event-badge">✏️ ИЗМЕНЕНО</div>
                <p class="muted">Было:</p>
                <p class="edit-old">«${escapeHtml(v.old_text || "—")}»</p>
                <p class="muted">Стало:</p>
                <p class="edit-new">«${escapeHtml(v.new_text || "—")}»</p>
                <p class="muted">${formatDate(v.edited_at)}</p>
            </div>
        `).join("");
        return `
            <div class="message-card">
                <div class="msg-meta">
                    <strong>${escapeHtml(m.sender_name || "Неизвестно")}</strong>
                    <span class="muted">${m.sender_username ? "@" + escapeHtml(m.sender_username) : ""}</span>
                </div>
                <p class="msg-text">${escapeHtml(m.text || "Содержимое недоступно")}</p>
                ${editsHtml}
                <div class="msg-footer">
                    <span>🕒 ${formatDate(m.sent_at)}</span>
                    ${m.media_type ? `<span class="badge">${m.media_type === "photo" ? "📷" : "🎬"}</span>` : ""}
                </div>
            </div>
        `;
    }).join("");
}

function renderMedia(data) {
    const items = data.media || [];
    const content = document.getElementById("chat-content");
    if (!items.length) {
        content.innerHTML = '<p class="empty-state">Нет доступных медиафайлов.</p>';
        return;
    }
    content.innerHTML = items.map(f => {
        if (!f.url) {
            return `<div class="media-card"><p>Файл недоступен</p><small class="muted">${formatDate(f.created_at)}</small></div>`;
        }
        const isVideo = f.media_type === "video";
        return `
            <div class="media-card">
                ${isVideo
                    ? `<video src="${f.url}" controls playsinline preload="metadata"></video>`
                    : `<img src="${f.url}" alt="Фото" loading="lazy">`}
                <small class="muted">${formatDate(f.created_at)}</small>
            </div>
        `;
    }).join("");
}

/* ── Навигация / UI ───────────────────────────────────────────────────── */
document.getElementById("back-btn").addEventListener("click", closeChat);

document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentTab = btn.dataset.tab;
        loadChatContent();
    });
});

document.getElementById("search-input").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadContacts(e.target.value.trim()), 300);
});

document.getElementById("settings-btn").addEventListener("click", () => {
    document.getElementById("settings-modal").classList.remove("hidden");
});
document.getElementById("close-settings").addEventListener("click", () => {
    document.getElementById("settings-modal").classList.add("hidden");
});

document.getElementById("delete-history-btn").addEventListener("click", async () => {
    if (!confirm("Удалить всю историю? Это действие необратимо.")) return;
    try {
        await apiDelete("/api/history");
        showToast("История удалена");
        document.getElementById("settings-modal").classList.add("hidden");
        loadStats();
        loadContacts();
    } catch (e) {
        showToast(e.message);
    }
});

/* Применение темы Telegram */
if (tg) {
    const bg = tg.themeParams?.bg_color;
    if (bg) document.body.style.background = "#" + bg;
}

loadStats();
loadContacts();
