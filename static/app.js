const history = []; // full conversation sent each turn
const messagesEl = document.getElementById("messages");
const emptyEl = document.getElementById("empty");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const modelEl = document.getElementById("model");
const whoEl = document.getElementById("who");
const logoutEl = document.getElementById("logout");

// Verify the user is signed in before showing the chat. A missing or
// expired cookie returns 401 here, which bounces back to the login page.
(async () => {
    try {
        const res = await fetch("/api/auth/me");
        if (!res.ok) {
            window.location.href = "/login";
            return;
        }
        const me = await res.json();
        whoEl.textContent = me.email;
    } catch {
        window.location.href = "/login";
    }
})();

logoutEl.addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", { method: "POST" }); }
    finally { window.location.href = "/login"; }
});

// Mobile-only account menu (display:contents on desktop, popup on mobile).
const accountToggle = document.getElementById("account-toggle");
const accountMenu = document.getElementById("account-menu");

function closeAccountMenu() {
    accountMenu.classList.remove("open");
    accountToggle.setAttribute("aria-expanded", "false");
}

accountToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = accountMenu.classList.toggle("open");
    accountToggle.setAttribute("aria-expanded", String(open));
});

// Click anywhere outside the menu (or its toggle) closes it.
document.addEventListener("click", (e) => {
    if (!accountMenu.contains(e.target) && e.target !== accountToggle) {
        closeAccountMenu();
    }
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && accountMenu.classList.contains("open")) {
        closeAccountMenu();
        accountToggle.focus();
    }
});

function escapeHtml(s) {
    return s.replace(/[&<>]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;" }[c]));
}

// Minimal, safe rendering: escape first, then add bold/code/rule highlights.
function render(text) {
    let h = escapeHtml(text);
    h = h.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\(?\b(\d{3}\.\d+[a-z]?)\)?/g, '<span class="rule">$1</span>');
    return h;
}

function addMessage(role, text, sources) {
    if (emptyEl) emptyEl.remove();

    const msg = document.createElement("div");
    msg.className = "msg " + (role === "user" ? "user" : "bot");
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = role === "user" ? escapeHtml(text) : render(text);

    if (sources && sources.length) {
        const det = document.createElement("details");
        det.className = "sources";
        det.innerHTML = "<summary>Rule sources (" + sources.length + ")</summary>";
        sources.forEach(s => {
            const c = document.createElement("span");
            c.className = "chip";
            c.innerHTML = "<b>" + escapeHtml(s.rule) + "</b> — " +
                            escapeHtml(s.preview) + "…";
            det.appendChild(c);
        });

        bubble.appendChild(det);
    }

    msg.appendChild(bubble);
    messagesEl.appendChild(msg);
    msg.scrollIntoView({ behavior: "smooth", block: "end" });

    return bubble;
}

async function send() {
    const text = inputEl.value.trim();

    if (!text) return;

    inputEl.value = ""; inputEl.style.height = "auto";
    sendEl.disabled = true;

    addMessage("user", text);
    history.push({ role: "user", content: text });

    const thinking = addMessage("bot", "");
    thinking.innerHTML = '<span class="dots"><span></span><span></span><span></span></span>';

    try {
        const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: history, model: modelEl.value }),
        });

        if (res.status === 401 || res.status === 403) {
            window.location.href = "/login";
            return;
        }

        const data = await res.json();
        thinking.innerHTML = render(data.answer || "(no answer)");

        if (data.sources && data.sources.length) {
            const det = document.createElement("details");
            det.className = "sources";
            det.innerHTML = "<summary>Rule sources (" + data.sources.length + ")</summary>";

            data.sources.forEach(s => {
                const c = document.createElement("span");
                c.className = "chip";
                c.innerHTML = "<b>" + escapeHtml(s.rule) + "</b> — " + escapeHtml(s.preview) + "…";
                det.appendChild(c);
            });

            thinking.appendChild(det);
        }

        history.push({ role: "assistant", content: data.answer || "" });
    } catch (e) {
        thinking.innerHTML = "Error reaching the server. Is it running?";
    } finally {
        sendEl.disabled = false;
        inputEl.focus();
    }
}

sendEl.onclick = send;
inputEl.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault(); send();
    }
});

inputEl.addEventListener("input", () => {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
});

document.querySelectorAll(".examples button").forEach(b => {
    b.onclick = () => { inputEl.value = b.textContent; send(); };
});
