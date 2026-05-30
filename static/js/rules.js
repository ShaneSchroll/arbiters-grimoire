/**
 * rules.js — the in-app rules documentation page.
 *
 * Loads /docs.json once, then runs entirely in the browser:
 *   - Builds the TOC sidebar (sections → subsections → rules)
 *   - Renders one section per heading in the main pane
 *   - Search filters TOC + highlights matches in the visible content
 *   - Cross-references like "see rule 509.2" become anchor links
 *   - URL hash (#rule-509.2) scrolls + highlights on load and on change
*/

(function () {

const tocEl     = document.getElementById("docs-toc");
const contentEl = document.getElementById("docs-content");
const mainEl    = document.getElementById("docs-main");
const searchEl  = document.getElementById("docs-search");

// Bail if this script ever loads on a page without the docs surface.
if (!tocEl || !contentEl) return;

init().catch(err => {
    contentEl.innerHTML =
        '<div class="docs-error">Could not load rules. ' +
        'Run <code>python ingest.py &lt;rulebook.pdf&gt;</code> to generate ' +
        '<code>docs.json</code>, then reload.</div>';
    console.error(err);
});

async function init() {
    const res = await fetch("/docs.json");
    if (!res.ok) throw new Error("docs.json " + res.status);
    const docs = await res.json();

    const tree = groupRules(docs);

    buildToc(tree, docs);
    buildContent(tree, docs);
    wireSearch(docs);
    wireScrollSpy();
    wireXrefClicks();
    wireSectionCollapse();
    wireMobileSidebar();
}

/*  data shaping  */

function ruleSortKey(r) {
    // "509.2a" → [509, 2, "a"]; lets us sort 100.2 < 100.10 properly.
    const m = /^(\d+)\.(\d+)([a-z]?)$/.exec(r);
    return m ? [+m[1], +m[2], m[3]] : [0, 0, ""];
}

function compareRules(a, b) {
    const ka = ruleSortKey(a.rule), kb = ruleSortKey(b.rule);
    return ka[0] - kb[0] || ka[1] - kb[1] || ka[2].localeCompare(kb[2]);
}

function groupRules(docs) {
    // {sectionId: {title, subsections: {subId: {title, rules: [...] }}}}
    const tree = {};
    for (const rule of docs.rules) {
        const sub = rule.rule.split(".")[0];          // "509"
        const sec = sub[0];                            // "5"
        const secNode = tree[sec] ||= {
            title: docs.sections[sec] || ("Section " + sec),
            subsections: {},
        };
        const subNode = secNode.subsections[sub] ||= {
            title: docs.subsections[sub] || ("Rule " + sub),
            rules: [],
        };
        subNode.rules.push(rule);
    }
    // Sort rules within each subsection.
    for (const sec of Object.values(tree)) {
        for (const sub of Object.values(sec.subsections)) {
            sub.rules.sort(compareRules);
        }
    }
    return tree;
}

/*  escaping + inline rendering */
function escapeHtml(s) {
    return s.replace(/[&<>]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;" }[c]));
}

// Same inline transforms as the chat bubble, but rule numbers become
// clickable cross-references that scroll to the target rule.
function renderInlineWithLinks(escaped) {
    let h = escaped;
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>");
    h = h.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<i>$2</i>");
    h = h.replace(
        /\(?\b(\d{3}\.\d+[a-z]?)\)?/g,
        (_, rule) =>
            '<a class="rule-xref" role="button" tabindex="0" data-rule="' +
            rule + '">' + rule + '</a>'
    );
    return h;
}

// PDF extraction leaves hard newlines mid-sentence. Same paragraph
// normalizer the chat modal uses, lifted here so rules.js is standalone.
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

function renderRuleBody(text) {
    return ruleParagraphs(text)
        .map(p => "<p>" + renderInlineWithLinks(escapeHtml(p)) + "</p>")
        .join("");
}

/*  TOC  */

function buildToc(tree, docs) {
    const parts = [];
    const sectionIds = Object.keys(tree).sort();
    for (const secId of sectionIds) {
        const sec = tree[secId];
        // Sections start collapsed: cleaner first impression, user picks
        // which area to drill into. Search auto-expands all via the
        // .docs-toc.searching class so matches stay visible.
        parts.push(
            '<div class="toc-section collapsed" data-section="' + secId + '">' +
                '<button class="toc-section-title" type="button" ' +
                        'aria-expanded="false" ' +
                        'aria-controls="toc-subs-' + secId + '">' +
                    // Empty span; styled as a mask-image in CSS so we can
                    // theme the color and rotate it on collapse state.
                    '<span class="toc-chevron" aria-hidden="true"></span>' +
                    '<span class="toc-num">' + secId + '.</span> ' +
                    escapeHtml(sec.title) +
                '</button>' +
                '<ul class="toc-subsections" id="toc-subs-' + secId + '">'
        );
        const subIds = Object.keys(sec.subsections).sort();
        for (const subId of subIds) {
            const sub = sec.subsections[subId];
            parts.push(
                '<li class="toc-subsection" data-subsection="' + subId + '">' +
                    '<a href="#sub-' + subId + '" class="toc-sub-link">' +
                        '<span class="toc-num">' + subId + '.</span> ' +
                        escapeHtml(sub.title) +
                    '</a>' +
                '</li>'
            );
        }
        parts.push("</ul></div>");
    }
    tocEl.innerHTML = parts.join("");
}

/*  main content  */

function buildContent(tree, docs) {
    const parts = [];
    const sectionIds = Object.keys(tree).sort();
    for (const secId of sectionIds) {
        const sec = tree[secId];
        parts.push(
            '<section class="docs-section" id="section-' + secId + '">' +
                '<h2 class="docs-section-title">' +
                    secId + '. ' + escapeHtml(sec.title) +
                '</h2>'
        );
        const subIds = Object.keys(sec.subsections).sort();
        for (const subId of subIds) {
            const sub = sec.subsections[subId];
            parts.push(
                '<section class="docs-subsection" id="sub-' + subId + '">' +
                    '<h3 class="docs-subsection-title">' +
                        subId + '. ' + escapeHtml(sub.title) +
                    '</h3>'
            );
            for (const rule of sub.rules) {
                parts.push(
                    '<article class="docs-rule" id="rule-' + rule.rule + '" ' +
                            'data-rule="' + rule.rule + '">' +
                        '<div class="docs-rule-head">' +
                            '<span class="docs-rule-num">' + rule.rule + '</span>' +
                        '</div>' +
                        '<div class="docs-rule-body">' +
                            renderRuleBody(rule.text) +
                        '</div>' +
                    '</article>'
                );
            }
            parts.push("</section>");
        }
        parts.push("</section>");
    }
    contentEl.innerHTML = parts.join("");
}

/*  search  */

let searchTimer = null;
function wireSearch(docs) {
    if (!searchEl) return;
    searchEl.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => runSearch(searchEl.value), 80);
    });
    // Escape clears.
    searchEl.addEventListener("keydown", e => {
        if (e.key === "Escape") { searchEl.value = ""; runSearch(""); }
    });
}

function runSearch(rawQuery) {
    const q = rawQuery.trim().toLowerCase();
    // When searching, CSS rule `.docs-toc.searching ... { display: block }`
    // overrides .collapsed so users see matching subsections without
    // having to expand sections manually first.
    tocEl.classList.toggle("searching", !!q);

    const rules = contentEl.querySelectorAll(".docs-rule");
    const subs  = contentEl.querySelectorAll(".docs-subsection");
    const secs  = contentEl.querySelectorAll(".docs-section");
    const tocSubs = tocEl.querySelectorAll(".toc-subsection");
    const tocSecs = tocEl.querySelectorAll(".toc-section");

    if (!q) {
        // Reset: show everything, drop highlights.
        rules.forEach(r => { r.classList.remove("hit"); r.style.display = ""; });
        subs.forEach(s => s.style.display = "");
        secs.forEach(s => s.style.display = "");
        tocSubs.forEach(t => t.style.display = "");
        tocSecs.forEach(t => t.style.display = "");
        clearHighlights();
        return;
    }

    // Match a rule if its number OR its body text contains the query.
    const matchedSubs = new Set();
    const matchedSecs = new Set();
    rules.forEach(rule => {
        const num = rule.dataset.rule.toLowerCase();
        const txt = rule.textContent.toLowerCase();
        const hit = num.includes(q) || txt.includes(q);
        rule.style.display = hit ? "" : "none";
        rule.classList.toggle("hit", hit);
        if (hit) {
            const sub = rule.closest(".docs-subsection");
            const sec = rule.closest(".docs-section");
            if (sub) matchedSubs.add(sub.id);
            if (sec) matchedSecs.add(sec.id);
        }
    });

    subs.forEach(s => s.style.display = matchedSubs.has(s.id) ? "" : "none");
    secs.forEach(s => s.style.display = matchedSecs.has(s.id) ? "" : "none");

    // Mirror to TOC: hide non-matching subsections + sections.
    tocSubs.forEach(t => {
        const subId = "sub-" + t.dataset.subsection;
        t.style.display = matchedSubs.has(subId) ? "" : "none";
    });
    tocSecs.forEach(t => {
        const secId = "section-" + t.dataset.section;
        t.style.display = matchedSecs.has(secId) ? "" : "none";
    });

    highlightMatches(q);
}

/*  highlighting  */

function clearHighlights() {
    contentEl.querySelectorAll("mark.search-hit").forEach(m => {
        const t = document.createTextNode(m.textContent);
        m.parentNode.replaceChild(t, m);
    });
    // Re-normalize so adjacent text nodes merge (cheap; small subtrees).
    contentEl.normalize();
}

function highlightMatches(q) {
    clearHighlights();
    if (!q) return;
    // Only walk currently-visible rules to keep this cheap.
    const visible = contentEl.querySelectorAll(".docs-rule.hit");
    const lower = q.toLowerCase();
    visible.forEach(node => walkAndWrap(node, lower));
}

function walkAndWrap(root, lower) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const targets = [];
    let n;
    while ((n = walker.nextNode())) {
        if (n.parentNode && n.parentNode.nodeName === "MARK") continue;
        if (n.nodeValue.toLowerCase().includes(lower)) targets.push(n);
    }
    for (const node of targets) wrapMatches(node, lower);
}

function wrapMatches(textNode, lower) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let i = 0;
    const lc = text.toLowerCase();
    while (i < text.length) {
        const at = lc.indexOf(lower, i);
        if (at === -1) {
            frag.appendChild(document.createTextNode(text.slice(i)));
            break;
        }
        if (at > i) frag.appendChild(document.createTextNode(text.slice(i, at)));
        const mark = document.createElement("mark");
        mark.className = "search-hit";
        mark.textContent = text.slice(at, at + lower.length);
        frag.appendChild(mark);
        i = at + lower.length;
    }
    textNode.parentNode.replaceChild(frag, textNode);
}

/*  scroll spy + URL hash  */

function wireScrollSpy() {
    if (!("IntersectionObserver" in window)) return;
    const rules = contentEl.querySelectorAll(".docs-rule, .docs-subsection");
    const tocLinks = new Map();  // id → toc <a> element
    tocEl.querySelectorAll(".toc-sub-link").forEach(a => {
        const href = a.getAttribute("href") || "";
        if (href.startsWith("#")) tocLinks.set(href.slice(1), a);
    });

    let active = null;
    const obs = new IntersectionObserver(entries => {
        for (const e of entries) {
            if (!e.isIntersecting) continue;
            // Find the closest enclosing subsection for the TOC pointer.
            const sub = e.target.closest(".docs-subsection");
            if (!sub) continue;
            const link = tocLinks.get(sub.id);
            if (!link || link === active) continue;
            if (active) active.classList.remove("toc-active");
            link.classList.add("toc-active");
            // Keep the active TOC item in view inside the sidebar.
            link.scrollIntoView({ block: "nearest" });
            active = link;
        }
    }, { root: mainEl, rootMargin: "-30% 0px -60% 0px", threshold: 0 });

    rules.forEach(r => obs.observe(r));
}

// In-page rule cross-references. Single delegated handler on the content
// root catches every .rule-xref click. We use getElementById (not
// querySelector) because rule ids contain "." which is invalid in a CSS
// selector. preventDefault keeps the URL clean - no hash navigation.
function wireXrefClicks() {
    contentEl.addEventListener("click", e => {
        const xref = e.target.closest(".rule-xref");
        if (!xref) return;
        e.preventDefault();
        scrollToRule(xref.dataset.rule);
    });
    // Keyboard parity: Enter / Space activates a focused xref.
    contentEl.addEventListener("keydown", e => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const xref = e.target.closest(".rule-xref");
        if (!xref) return;
        e.preventDefault();
        scrollToRule(xref.dataset.rule);
    });
}

function scrollToRule(rule) {
    const el = document.getElementById("rule-" + rule);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1400);
}

/*  collapsible TOC sections  */

// Delegated handler: clicking a section title toggles its .collapsed
// state. CSS handles the chevron rotation and the subsection visibility.
function wireSectionCollapse() {
    tocEl.addEventListener("click", e => {
        const title = e.target.closest(".toc-section-title");
        if (!title) return;
        const section = title.closest(".toc-section");
        if (!section) return;
        const willCollapse = !section.classList.contains("collapsed");
        section.classList.toggle("collapsed", willCollapse);
        title.setAttribute("aria-expanded", String(!willCollapse));
    });
}

/*  mobile sidebar drawer  */

// On mobile (<=900px) the sidebar is hidden behind a fixed toggle button
// and slides in over the content with a dimmed backdrop. Desktop ignores
// the .open class entirely - the sidebar is always visible in the grid.
function wireMobileSidebar() {
    const toggle  = document.getElementById("docs-sidebar-toggle");
    const sidebar = document.getElementById("docs-sidebar");
    const backdrop = document.getElementById("docs-backdrop");
    if (!toggle || !sidebar || !backdrop) return;

    function setOpen(open) {
        sidebar.classList.toggle("open", open);
        backdrop.classList.toggle("open", open);
        toggle.setAttribute("aria-expanded", String(open));
        backdrop.setAttribute("aria-hidden", String(!open));
    }

    toggle.addEventListener("click", () => {
        setOpen(!sidebar.classList.contains("open"));
    });
    backdrop.addEventListener("click", () => setOpen(false));

    // Tapping a subsection link auto-closes the drawer so the user lands
    // on the content they picked instead of staring at the TOC.
    sidebar.addEventListener("click", e => {
        if (e.target.closest(".toc-sub-link")) setOpen(false);
    });

    // Escape closes the drawer when it's open.
    document.addEventListener("keydown", e => {
        if (e.key === "Escape" && sidebar.classList.contains("open")) {
            setOpen(false);
            toggle.focus();
        }
    });

    // If the viewport grows back to desktop while the drawer is open,
    // strip the open state so the desktop layout isn't stuck with a
    // backdrop visible.
    const mq = window.matchMedia("(min-width: 901px)");
    mq.addEventListener("change", e => { if (e.matches) setOpen(false); });
}

})();   // end IIFE
