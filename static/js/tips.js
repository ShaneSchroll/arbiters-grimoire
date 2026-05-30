// Tips Page
(function () {
    "use strict";

    const LS_KEY = "oracle.tips.layout";

    function ready(fn) {
        if (document.readyState !== "loading") fn();
        else document.addEventListener("DOMContentLoaded", fn);
    }

    ready(function () {
        const list = document.querySelector(".codex-tips");
        const toggleEl = document.querySelector(".codex-layout-toggle");
        if (!list || !toggleEl) return;

        // ── Restore saved layout ────────────────────────────────────────────
        // Falls back to the data-layout attribute already on the markup so the
        // default (magazine) is owned by the HTML, not the JS.
        const saved = safeRead(LS_KEY);
        if (saved === "magazine" || saved === "cards") {
            setLayout(saved);
        } else {
            // Keep markup-default in sync with toggle button aria-pressed.
            setLayout(list.getAttribute("data-layout") || "magazine");
        }

        // ── Layout toggle clicks ────────────────────────────────────────────
        toggleEl.addEventListener("click", function (e) {
            const btn = e.target.closest("button[data-layout]");
            if (!btn) return;
            const next = btn.getAttribute("data-layout");
            if (next === list.getAttribute("data-layout")) return;
            setLayout(next);
            safeWrite(LS_KEY, next);
        });

        // ── Expand / collapse ───────────────────────────────────────────────
        // Event delegation so we don't bind 15 handlers; also gives us free
        // access to clicks on the tip title (which we treat as a second toggle
        // affordance so the whole header is clickable).
        list.addEventListener("click", function (e) {
            const tip = e.target.closest(".codex-tip");
            if (!tip) return;

            const toggleBtn = e.target.closest(".codex-tip-toggle");
            const titleEl = e.target.closest(".codex-tip-title");
            if (!toggleBtn && !titleEl) return;

            toggleTip(tip);
        });

        // Keyboard: Enter / Space on a focused tip title also opens the panel.
        list.addEventListener("keydown", function (e) {
            if (e.key !== "Enter" && e.key !== " ") return;
            const titleEl = e.target.closest(".codex-tip-title");
            if (!titleEl) return;
            e.preventDefault();
            const tip = titleEl.closest(".codex-tip");
            if (tip) toggleTip(tip);
        });

        // Make titles focusable so the keyboard handler above actually fires.
        list.querySelectorAll(".codex-tip-title").forEach(function (el) {
            if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "0");
            el.setAttribute("role", "button");
        });

        // Helpers

        function setLayout(mode) {
            list.setAttribute("data-layout", mode);
            toggleEl.querySelectorAll("button[data-layout]").forEach(function (b) {
                const on = b.getAttribute("data-layout") === mode;
                b.setAttribute("aria-pressed", String(on));
            });

            // Collapse everything when switching to cards
            if (mode === "cards") {
                list.querySelectorAll(".codex-tip[data-open='true']").forEach(closeTip);
            }
        }

        function toggleTip(tip) {
            if (tip.getAttribute("data-open") === "true") closeTip(tip);
            else openTip(tip);
        }

        function openTip(tip) {
            tip.setAttribute("data-open", "true");
            const btn = tip.querySelector(".codex-tip-toggle");
            if (btn) btn.setAttribute("aria-expanded", "true");
            const title = tip.querySelector(".codex-tip-title");
            if (title) title.setAttribute("aria-expanded", "true");
        }

        function closeTip(tip) {
            tip.setAttribute("data-open", "false");
            const btn = tip.querySelector(".codex-tip-toggle");
            if (btn) btn.setAttribute("aria-expanded", "false");
            const title = tip.querySelector(".codex-tip-title");
            if (title) title.setAttribute("aria-expanded", "false");
        }

        // localStorage with quiet failure for private browsing
        function safeRead(k) {
            try { return window.localStorage.getItem(k); } catch (_) { return null; }
        }
        function safeWrite(k, v) {
            try { window.localStorage.setItem(k, v); } catch (_) { /* ignore */ }
        }
    });
})();
