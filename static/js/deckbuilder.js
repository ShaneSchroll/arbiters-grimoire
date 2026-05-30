// deckbuilder.js — Deck Builder page logic (iterative).
(function () {
    const decklistEl = document.getElementById("decklist");
    const formatEl = document.getElementById("format");
    const notesEl = document.getElementById("notes");
    const buildEl = document.getElementById("build");
    const transcriptEl = document.getElementById("transcript");
    const followupRow = document.getElementById("followup-row");
    const followupEl = document.getElementById("followup");
    const followupSend = document.getElementById("followup-send");
    const modelEl = document.getElementById("model");
    if (!decklistEl || !buildEl) return; // not the deck builder page

    // Full conversation, replayed to the backend each turn.
    let history = [];
    let busy = false;

    const renderMd =
        (typeof window.render === "function" && window.render) ||
        function (t) {
            return String(t)
                .replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))
                .replace(/\n/g, "<br>");
        };
    const escapeText = (typeof window.escapeHtml === "function" && window.escapeHtml) ||
        (s => String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])));

    // Parse the textarea into [{name, count}]. Accepts "3 Lightning Bolt",
    // "3x Lightning Bolt", or just "Lightning Bolt"
    function parseDecklist(text) {
        const out = [];
        for (const raw of text.split("\n")) {
            let line = raw.trim();
            if (!line || line.startsWith("//") || line.startsWith("#")) continue;
            const m = line.match(/^(\d+)\s*[xX]?\s+(.*)$/);
            let count = 1, name = line;
            if (m) { count = Math.min(parseInt(m[1], 10) || 1, 99); name = m[2]; }
            name = name.replace(/\s*\([A-Za-z0-9]{2,5}\)\s*[\d-]*\s*$/, "")
                       .replace(/\s*\*[^*]*\*\s*$/, "")
                       .trim();
            if (name) out.push({ name: name.slice(0, 120), count });
        }
        return out;
    }

    function collectForm() {
        return {
            deck: parseDecklist(decklistEl.value),
            fmt: formatEl ? formatEl.value : "",
            notes: notesEl ? notesEl.value.trim() : "",
        };
    }

    // Append a bubble to the transcript, mirroring the chat page's classes.
    function addBubble(role, html) {
        const msg = document.createElement("div");
        msg.className = "msg " + (role === "user" ? "user" : "bot");
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        bubble.innerHTML = html;
        msg.appendChild(bubble);
        transcriptEl.appendChild(msg);
        msg.scrollIntoView({ behavior: "smooth", block: "end" });
        return bubble;
    }

    function setBusy(b) {
        busy = b;
        buildEl.disabled = b;
        if (followupSend) followupSend.disabled = b;
    }

    // Run one turn: show the user's text, stream the assistant reply, and on
    // completion record both into history. `userText` is what we send + show.
    async function runTurn(userText) {
        if (busy) return;
        const form = collectForm();
        if (!form.deck.length) {
            addBubble("bot", "<p><i>Add at least one card to your decklist first.</i></p>");
            return;
        }

        setBusy(true);
        addBubble("user", escapeText(userText));
        history.push({ role: "user", content: userText });

        const bot = addBubble("bot",
            '<span class="dots"><span></span><span></span><span></span></span>');

        let accumulated = "";
        let firstDelta = true;
        let renderPending = false;
        function scheduleRender() {
            if (renderPending) return;
            renderPending = true;
            requestAnimationFrame(() => {
                renderPending = false;
                bot.innerHTML = renderMd(accumulated);
                bot.scrollIntoView({ behavior: "smooth", block: "end" });
            });
        }

        try {
            const res = await fetch("/api/deckbuilder", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    deck: form.deck,
                    fmt: form.fmt,
                    notes: form.notes,
                    messages: history,
                    model: modelEl ? modelEl.value : "claude-sonnet-4-6",
                }),
            });

            if (res.status === 401 || res.status === 403) {
                window.location.href = "/login";
                return;
            }
            if (res.status === 429) {
                const body = await res.json().catch(() => ({}));
                bot.innerHTML = "<p><i>" +
                    escapeText(body.detail || "Rate or budget limit reached.") + "</i></p>";
                history.pop(); // don't keep a user turn that got no reply
                return;
            }
            if (!res.ok || !res.body) throw new Error("Bad response: " + res.status);

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf("\n\n")) !== -1) {
                    const frame = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    const dataLine = frame.split("\n").find(l => l.startsWith("data:"));
                    if (!dataLine) continue;
                    let evt;
                    try { evt = JSON.parse(dataLine.slice(5).trim()); }
                    catch { continue; }

                    if (evt.type === "delta") {
                        if (firstDelta) { bot.innerHTML = ""; firstDelta = false; }
                        accumulated += evt.text;
                        scheduleRender();
                    } else if (evt.type === "error") {
                        accumulated += (accumulated ? "\n\n" : "") + "*" + evt.message + "*";
                        scheduleRender();
                    }
                    // "done" carries only {model}; nothing extra to render.
                }
            }

            bot.innerHTML = renderMd(accumulated || "(no suggestions)");
            history.push({ role: "assistant", content: accumulated });
            followupRow.classList.add("show");      // reveal refine box after 1st turn
            if (typeof window.refreshMe === "function") window.refreshMe();
        } catch (e) {
            bot.innerHTML = "<p><i>Error reaching the server. Is it running?</i></p>";
            history.pop();
        } finally {
            setBusy(false);
        }
    }

    // "Finish My Deck": start a brand-new conversation from the current form.
    function startBuild() {
        if (busy) return;
        history = [];
        transcriptEl.innerHTML = "";
        followupRow.classList.remove("show");
        runTurn("Analyze my deck and suggest what to add to finish it and what to cut.");
    }

    function sendFollowup() {
        const text = followupEl.value.trim();
        if (!text) return;
        followupEl.value = "";
        followupEl.style.height = "auto";
        runTurn(text);
    }

    buildEl.addEventListener("click", startBuild);
    followupSend.addEventListener("click", sendFollowup);
    followupEl.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFollowup(); }
    });
    followupEl.addEventListener("input", () => {
        followupEl.style.height = "auto";
        followupEl.style.height = Math.min(followupEl.scrollHeight, 140) + "px";
    });
})();
