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

// Inline-only transforms (assumes input is already HTML-escaped).
// Order matters: code first so backtick contents aren't reinterpreted,
// then bold (**), then italic (*), then rule-number highlights.
function renderInline(escaped) {
    let h = escaped;
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>");
    h = h.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<i>$2</i>");
    h = h.replace(/\(?\b(\d{3}\.\d+[a-z]?)\)?/g, '<span class="rule">$1</span>');
    return h;
}

// A line that starts a block element (heading, list item, code fence, or
// horizontal rule). Used so paragraph collection stops at the next block.
const BLOCK_START = /^(?:#{1,4}\s|[-*+]\s|\d+\.\s|```|-{3,}\s*$)/;
const HR_LINE = /^-{3,}\s*$/;

// Minimal block-level markdown: paragraphs (blank-line separated), ATX
// headings (#..####), unordered/ordered lists, fenced ``` code blocks,
// plus the inline transforms above. Intentionally small - just enough to
// render what Claude actually emits in chat. NOT a spec-compliant parser.
function render(text) {
    const lines = text.split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
        const line = lines[i];
        const stripped = line.trim();

        // Fenced code block - render verbatim, no inline transforms inside.
        if (/^```/.test(stripped)) {
            i++;
            const buf = [];
            while (i < lines.length && !/^```/.test(lines[i].trim())) {
                buf.push(lines[i]);
                i++;
            }
            i++; // consume closing fence (or run off the end)
            out.push("<pre><code>" + escapeHtml(buf.join("\n")) + "</code></pre>");
            continue;
        }

        // Horizontal rule (---). Checked before lists so it isn't eaten
        // as a one-character "-" bullet.
        if (HR_LINE.test(stripped)) {
            out.push("<hr>");
            i++;
            continue;
        }

        // ATX heading: # → h3, ## → h4, ### → h5, #### → h6
        const heading = /^(#{1,4})\s+(.*)$/.exec(line);
        if (heading) {
            const level = heading[1].length + 2;
            out.push(
                "<h" + level + ">" +
                renderInline(escapeHtml(heading[2].trim())) +
                "</h" + level + ">"
            );
            i++;
            continue;
        }

        // Unordered list
        if (/^[-*+]\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
                const item = lines[i].replace(/^[-*+]\s+/, "");
                items.push("<li>" + renderInline(escapeHtml(item)) + "</li>");
                i++;
            }
            out.push("<ul>" + items.join("") + "</ul>");
            continue;
        }

        // Ordered list
        if (/^\d+\.\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
                const item = lines[i].replace(/^\d+\.\s+/, "");
                items.push("<li>" + renderInline(escapeHtml(item)) + "</li>");
                i++;
            }
            out.push("<ol>" + items.join("") + "</ol>");
            continue;
        }

        // Blank line - paragraph separator
        if (!stripped) { i++; continue; }

        // Paragraph: gather contiguous lines until blank or a new block.
        const para = [];
        while (
            i < lines.length &&
            lines[i].trim() &&
            !BLOCK_START.test(lines[i])
        ) {
            para.push(lines[i].trim());
            i++;
        }
        out.push("<p>" + renderInline(escapeHtml(para.join(" "))) + "</p>");
    }

    return out.join("");
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
        .map(p => "<p>" + renderInline(escapeHtml(p)) + "</p>")
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
