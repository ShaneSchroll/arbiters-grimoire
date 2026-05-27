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

// Chip preview length. Keeps chips skim-able; full text is in the modal.
const CHIP_TEXT_MAX = 120;

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

// Collapse whitespace and clip to max chars; appends an ellipsis if clipped.
function chipPreview(text) {
    const flat = text.trim().replace(/\s+/g, " ");
    return flat.length > CHIP_TEXT_MAX
        ? flat.slice(0, CHIP_TEXT_MAX).trimEnd() + "…"
        : flat;
}

// PDF extraction leaves hard newlines mid-sentence. Re-flow into paragraphs:
// a line only starts a new paragraph if it begins with a rule number
// (e.g. "120.5", "120.5a") or a labeled callout ("Example:").
const PARA_START = /^(?:\d{3}\.\d+[a-z]?\.?\s|Example[:\s])/;

function ruleParagraphs(text) {
    const out = [];
    let buf = "";
    for (const raw of text.split("\n")) {
        const line = raw.trim();
        if (!line) continue;
        if (buf && PARA_START.test(line)) {
            out.push(buf);
            buf = line;
        } else {
            buf = buf ? buf + " " + line : line;
        }
    }
    if (buf) out.push(buf);
    return out;
}

function renderRule(text) {
    return ruleParagraphs(text)
        .map(p => "<p>" + render(p) + "</p>")
        .join("");
}

function openRuleModal(rule, text) {
    const backdrop = document.createElement("div");
    backdrop.className = "rule-modal-backdrop";
    backdrop.innerHTML =
        '<div class="rule-modal" role="dialog" aria-modal="true" aria-labelledby="rule-modal-title">' +
            '<button class="rule-modal-close" type="button" aria-label="Close">&times;</button>' +
            '<h3 class="rule-modal-title" id="rule-modal-title">' + escapeHtml(rule) + '</h3>' +
            '<div class="rule-modal-body">' + renderRule(text) + '</div>' +
        '</div>';

    function close() {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
    }
    function onKey(e) { if (e.key === "Escape") close(); }

    backdrop.addEventListener("click", e => {
        if (e.target === backdrop) close();
    });
    backdrop.querySelector(".rule-modal-close").addEventListener("click", close);
    document.addEventListener("keydown", onKey);

    document.body.appendChild(backdrop);
    backdrop.querySelector(".rule-modal-close").focus();
}

function appendSources(bubble, sources) {
    if (!sources || !sources.length) return;
    const det = document.createElement("details");
    det.className = "sources";
    det.innerHTML = "<summary>Rule sources (" + sources.length + ")</summary>";
    sources.forEach(s => {
        const chip = document.createElement("button");
        chip.className = "chip";
        chip.type = "button";
        chip.title = "Click to view full rule text";
        chip.innerHTML = "<b>" + escapeHtml(s.rule) + "</b> — " +
                        escapeHtml(chipPreview(s.text || ""));
        chip.addEventListener("click", () => openRuleModal(s.rule, s.text || ""));
        det.appendChild(chip);
    });
    bubble.appendChild(det);
}

function addMessage(role, text, sources) {
    if (emptyEl) emptyEl.remove();

    const msg = document.createElement("div");
    msg.className = "msg " + (role === "user" ? "user" : "bot");
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = role === "user" ? escapeHtml(text) : render(text);

    appendSources(bubble, sources);

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
        appendSources(thinking, data.sources);
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
