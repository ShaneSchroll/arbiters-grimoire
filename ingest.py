"""
ingest.py - Preprocess the official MTG Comprehensive Rules into search JSON.

Wizards publishes the Comprehensive Rules as a plain-text file: one rule or
subrule per line, blank-line separated. Parsing that is far more reliable than
scraping the old PDF - there are no page headers/footers, no mid-sentence line
wraps, and rule/section boundaries are unambiguous. Grab the .txt from
https://magic.wizards.com/en/rules.

Run once, and again whenever the rules update:

    python ingest.py rules.txt

It writes two files next to this script:
  - rules.json : one chunk per base rule (its subrules and examples folded in),
                 plus one chunk per glossary term. retriever.py BM25-searches it.
  - docs.json  : section/subsection titles + per-rule text, for the in-app
                 rules page (/pages/rules).

UTF-8 throughout: the rules contain characters like the real minus sign
(U+2212) and curly quotes that Windows' default cp1252 codec can't write.
"""

import json
import re
import sys
from pathlib import Path

# "1. Game Concepts" - one of the nine top-level sections.
SECTION = re.compile(r"^([1-9])\.\s+(\S.*)$")
# "100. General" - a three-digit subsection heading (no rule number follows).
SUBSECTION = re.compile(r"^(\d{3})\.\s+(\S.*)$")
# "100.1. ..." - a base rule. The period is optional to tolerate the occasional
# source typo (e.g. "606.5 ..."). Subrules ("100.1a ...") and "Example:" lines
# intentionally do NOT match: they fold into the base rule's chunk.
BASE_RULE = re.compile(r"^(\d{3}\.\d+)\.?\s")
# "These rules are effective as of June 19, 2026." - the CR's version stamp,
# in the preamble above the first rule. Captures the date string.
EFFECTIVE_DATE = re.compile(r"effective as of\s+(.+?)\.?\s*$", re.IGNORECASE)


def find_effective_date(preamble: list[str]) -> str | None:
    """The date string from the CR's 'These rules are effective as of ...' line,
    or None if it's absent (e.g. a source file trimmed above the Introduction).
    Only the preamble (before the first rule) is scanned so a rule that happens
    to contain the phrase can't be mistaken for the version stamp."""
    for ln in preamble:
        m = EFFECTIVE_DATE.search(ln.strip())
        if m:
            return m.group(1).strip()
    return None


def split_body_and_glossary(lines: list[str]) -> tuple[int, int]:
    """Find where the rules body and the glossary begin.

    The file opens with an Introduction and a CONTENTS table that repeats every
    section/subsection title (plus the words "Glossary" and "Credits"). We skip
    all of it by anchoring on the first real rule line, then stepping back over
    the headings that introduce it so the first section/subsection titles still
    get captured. The glossary is the last line that is exactly "Glossary".
    """
    first_rule = next(
        (i for i, ln in enumerate(lines) if BASE_RULE.match(ln.strip())), None
    )
    if first_rule is None:
        raise ValueError(
            "No rule lines found - is this the Comprehensive Rules .txt?"
        )

    body_start = first_rule
    j = first_rule - 1
    while j >= 0:
        s = lines[j].strip()
        if SECTION.match(s) or SUBSECTION.match(s):
            body_start = j
        elif s:
            break  # a non-heading, non-blank line: the CONTENTS tail
        j -= 1

    glossary_start = max(
        (i for i, ln in enumerate(lines) if ln.strip() == "Glossary"),
        default=len(lines),
    )
    return body_start, glossary_start


def parse_rules(body: list[str]):
    """Walk the rules body once, returning (chunks, sections, subsections).

    One chunk per base rule, with its subrules and examples folded in. Section
    and subsection headings become titles - never text appended to the previous
    rule. (Appending them was the PDF bug that left a rule chunk ending with the
    next rule's heading.)
    """
    chunks: list[dict] = []
    sections: dict[str, str] = {}
    subsections: dict[str, str] = {}
    current: dict | None = None

    def flush():
        nonlocal current
        if current:
            chunks.append({
                "id": f"rule-{current['rule']}",
                "rule": current["rule"],
                "text": "\n".join(current["lines"]).strip(),
            })
        current = None

    for raw in body:
        line = raw.strip()
        if not line:
            continue
        m_rule = BASE_RULE.match(line)
        if m_rule:
            flush()
            current = {"rule": m_rule.group(1), "lines": [line]}
        elif m_sec := SECTION.match(line):
            flush()
            sections[m_sec.group(1)] = m_sec.group(2).strip()
        elif m_sub := SUBSECTION.match(line):
            flush()
            subsections[m_sub.group(1)] = m_sub.group(2).strip()
        elif current is not None:
            current["lines"].append(line)  # subrule, example, or continuation

    flush()
    return chunks, sections, subsections


def parse_glossary(gloss: list[str]):
    """One chunk per glossary term: the term line plus its definition line(s),
    blank-line separated. Per-term chunks retrieve far better than fixed-size
    blobs for "what does <keyword> mean" questions."""
    chunks: list[dict] = []
    block: list[str] = []

    for raw in [*gloss, ""]:  # trailing "" flushes the final block
        line = raw.strip()
        if line:
            block.append(line)
            continue
        if block:
            slug = re.sub(r"[^a-z0-9]+", "-", block[0].lower()).strip("-")
            chunks.append({
                "id": f"glossary-{slug or len(chunks)}",
                "rule": None,
                "text": "\n".join(block),
            })
            block = []
    return chunks


def main():
    if len(sys.argv) != 2:
        print("Usage: python ingest.py rules.txt")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    print(f"Reading {src} ...")
    lines = src.read_text(encoding="utf-8").splitlines()

    body_start, glossary_start = split_body_and_glossary(lines)
    effective_date = find_effective_date(lines[:body_start])
    rules, sections, subsections = parse_rules(lines[body_start:glossary_start])
    glossary = parse_glossary(lines[glossary_start + 1:])

    here = Path(__file__).parent

    rules_out = here / "rules.json"
    rules_out.write_text(
        json.dumps(rules + glossary, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    print(
        f"Wrote {rules_out} "
        f"({len(rules)} rules + {len(glossary)} glossary entries)."
    )

    docs_out = here / "docs.json"
    docs_out.write_text(
        json.dumps(
            {
                "effective_date": effective_date,
                "sections": sections,
                "subsections": subsections,
                "rules": [{"rule": c["rule"], "text": c["text"]} for c in rules],
            },
            ensure_ascii=False,
            indent=0,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote {docs_out} "
        f"({len(sections)} sections, {len(subsections)} subsections, "
        f"{len(rules)} rules; effective date: {effective_date or 'not found'})."
    )


if __name__ == "__main__":
    main()
