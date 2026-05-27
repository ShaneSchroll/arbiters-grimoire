"""
ingest.py - One-time preprocessing of your MTG rulebook PDF.

Run this once (or when the rules change - official magic rules pdf used):

    python ingest.py path/to/MagicCompRules.pdf

It extracts the text, splits it into retrievable chunks, and writes
`index.json` next to this script. The server reads that file at startup.

The chunker is tuned for the official Magic Comprehensive Rules (rules are
numbered like "509.2" with lettered subrules like "509.2a"). If your PDF is a
prose-style rulebook instead, it automatically falls back to fixed-size
overlapping chunks, so it works either way.
"""

import json
import re
import sys
from pathlib import Path

import pdfplumber

RULE_START = re.compile(r"^(\d{3}\.\d+)\.?\s")          # base rule, e.g. "509.2."

# Top-level sections in the Comprehensive Rules look like "1. Game Concepts",
# "5. The Combat Phase", etc. Subsections look like "509. Declare Attackers
# Step" (3 digits, no second number). These are what the docs page uses to
# group rules in the TOC.
SECTION_START    = re.compile(r"^([1-9])\.\s+([A-Z][^\d].*)$")
SUBSECTION_START = re.compile(r"^(\d{3})\.\s+([A-Z][^\d].*)$")

# A line that is *only* the word Glossary or Credits - the real section headings.
STOP_SECTION = re.compile(r"^(glossary|credits)\s*$", re.IGNORECASE)


def extract_text(pdf_path: str) -> str:
    """Pull raw text out of every page of the PDF."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            pages.append(page.extract_text() or "")
            print(f"  extracted page {i + 1}/{len(pdf.pages)}", end="\r")
    print()
    return "\n".join(pages)


def chunk_rules(text: str):
    """
    Split Comprehensive-Rules text into one chunk per base rule (subrules
    included), so an entire rule and its clarifications stay together.

    Returns a list of {"id", "rule", "text"} dicts, or [] if the text does
    not look like the Comprehensive Rules.
    """
    lines = text.splitlines()
    chunks = []
    current = None  # {"rule": str, "lines": [str]}
    seen = 0        # how many rules we've started - guards against TOC noise

    for line in lines:
        stripped = line.strip()
        m = RULE_START.match(stripped)
        if m:
            seen += 1
            if current:
                chunks.append(current)
            current = {"rule": m.group(1), "lines": [stripped]}
        elif current is not None:
            # The real Glossary/Credits sections come AFTER all the numbered
            # rules. Stop here so they don't bloat the final rule's chunk.
            # The `seen > 50` guard ignores the identical words that appear
            # in the table of contents before any rules have started.
            if STOP_SECTION.match(stripped) and seen > 50:
                break
            current["lines"].append(stripped)

    if current:
        chunks.append(current)

    if len(chunks) < 20:
        return []  # not the Comprehensive Rules - caller should fall back

    return [
        {
            "id": f"rule-{c['rule']}",
            "rule": c["rule"],
            "text": "\n".join(l for l in c["lines"] if l).strip(),
        }
        for c in chunks
    ]


def extract_sections(text: str):
    """Walk the PDF text once, pulling out the section + subsection titles
    used as TOC labels on the docs page.

    Stops at the real Glossary heading. The Glossary contains terms with
    numbered definitions (e.g. "Vanguard\\n1. A casual variant..."). Without
    the stop, those "1." / "2." / "3." lines match SECTION_START and
    clobber the real "Game Concepts" / "Parts of a Card" / "Card Types"
    titles. Sections 4-9 survive only because no glossary entry happens to
    have that many numbered definitions. The `seen_rules > 50` guard
    mirrors chunk_rules() and ignores the word "Glossary" that appears in
    the table of contents before any rules have started.

    A line that matches both as a section *and* as a subsection (or appears
    twice - once in the table of contents and again at its actual location)
    is fine: we just overwrite with the same value.

    Returns two dicts:
        sections    -> {"5": "The Combat Phase", ...}
        subsections -> {"509": "Declare Attackers Step", ...}
    """
    sections, subsections = {}, {}
    seen_rules = 0
    for line in text.splitlines():
        stripped = line.strip()
        if STOP_SECTION.match(stripped) and seen_rules > 50:
            break
        if RULE_START.match(stripped):
            seen_rules += 1
            continue  # skip rules - they look like "509.2. ..." not titles
        m_sub = SUBSECTION_START.match(stripped)
        if m_sub:
            subsections[m_sub.group(1)] = m_sub.group(2).strip()
            continue
        m_sec = SECTION_START.match(stripped)
        if m_sec:
            sections[m_sec.group(1)] = m_sec.group(2).strip()
    return sections, subsections


def write_docs_json(text: str, rule_chunks: list, out_path: Path):
    """Write a docs-friendly JSON: section + subsection titles plus the
    per-rule chunks. The browser uses this to render the rules docs page
    without needing the BM25-overlapping glossary chunks in index.json."""
    sections, subsections = extract_sections(text)
    docs = {
        "sections": sections,
        "subsections": subsections,
        "rules": [
            {"rule": c["rule"], "text": c["text"]}
            for c in rule_chunks if c.get("rule")
        ],
    }
    out_path.write_text(
        json.dumps(docs, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    print(
        f"Wrote {out_path} "
        f"({len(sections)} sections, {len(subsections)} subsections, "
        f"{len(docs['rules'])} rules)."
    )


def chunk_fixed(text: str, size: int = 1100, overlap: int = 150):
    """
    Generic overlapping chunker for prose PDFs (or the glossary tail).

    Splits on blank lines when the PDF has them; otherwise falls back to
    splitting on single newlines, so it never produces one giant chunk.
    """
    units = [u.strip() for u in re.split(r"\n\s*\n", text) if u.strip()]
    if len(units) < 5:  # PDF had no blank lines between paragraphs
        units = [u.strip() for u in text.splitlines() if u.strip()]

    chunks, buf = [], ""
    for unit in units:
        if len(buf) + len(unit) + 1 > size and buf:
            chunks.append(buf.strip())
            buf = buf[-overlap:] + " " + unit
        else:
            buf = (buf + "\n" + unit) if buf else unit
    if buf.strip():
        chunks.append(buf.strip())

    return [
        {"id": f"chunk-{i:04d}", "rule": None, "text": c}
        for i, c in enumerate(chunks)
    ]


def main():
    if len(sys.argv) != 2:
        print("Usage: python ingest.py path/to/rulebook.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]
    if not Path(pdf_path).exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    print(f"Reading {pdf_path} ...")
    text = extract_text(pdf_path)

    rule_hits = sum(1 for ln in text.splitlines() if RULE_START.match(ln.strip()))
    print(f"Lines that look like a rule number: {rule_hits}")

    chunks = chunk_rules(text)
    rule_chunks_only = list(chunks)  # snapshot before the glossary is appended
    if chunks:
        # Append the trailing Glossary as fixed-size chunks too. Use the LAST
        # "Glossary" line (the real heading), not the table-of-contents entry.
        gloss = list(re.finditer(r"(?mi)^\s*glossary\s*$", text))
        if gloss:
            chunks += chunk_fixed(text[gloss[-1].start():])
        print(f"Detected Comprehensive Rules: {len(chunks)} chunks.")
    else:
        chunks = chunk_fixed(text)
        rule_chunks_only = []
        print(f"Prose / unstructured rulebook: {len(chunks)} fixed-size chunks.")

    out = Path(__file__).parent / "index.json"
    # encoding="utf-8" is required: the CR contains characters like the real
    # minus sign (U+2212) that Windows' default cp1252 codec cannot write.
    out.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    print(f"Wrote {out} ({len(chunks)} chunks).")

    # docs.json powers the in-app /pages/rules documentation page. It's only
    # meaningful when the input looked like the Comprehensive Rules, so we
    # skip it for the prose fallback case (no rule numbers => no TOC).
    if rule_chunks_only:
        write_docs_json(
            text, rule_chunks_only, Path(__file__).parent / "docs.json"
        )


if __name__ == "__main__":
    main()
