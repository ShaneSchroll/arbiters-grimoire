"""
build_card_cache.py — Download Scryfall's `oracle_cards` bulk file once and
build a local SQLite card cache for instant, offline lookups.

This is the cards-side analogue of ingest.py: a one-shot build step that
produces an artifact (cards.db) the running server loads read-only via
card_cache.CardCache — exactly how Retriever loads index.json.

Why bulk data instead of looping the API:
  Scryfall explicitly asks you NOT to fetch cards one-by-one for catalog-scale
  work. The `oracle_cards` file is one object per functionally-unique card
  (~30k rows), updated roughly every 12h. Gameplay text changes rarely, so a
  weekly rebuild (or a rebuild after a set release) is plenty.

Usage:
    python build_card_cache.py                 # build ./cards.db
    python build_card_cache.py --out cards.db  # custom path
    python build_card_cache.py --force         # rebuild even if fresh

Run it in your deploy/build step (next to `python ingest.py <pdf>`), or behind
the admin panel / a cron job. No new dependencies: httpx is already in your
requirements, sqlite3 ships with Python.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import httpx

SCRYFALL = "https://api.scryfall.com"
BULK_TYPE = "oracle_cards"  # one row per gameplay-unique card
USER_AGENT = "mtg-rules-assistant/1.0"
ACCEPT = "application/json;q=0.9,*/*;q=0.8"  # Scryfall asks clients to send Accept
DEFAULT_OUT = Path(__file__).resolve().parent / "cards.db"


def normalize_name(name: str) -> str:
    """Casefold + strip accents/punctuation noise so 'Lim-Dûl's Vault',
    'lim-dul's vault' and 'Lim-Dul’s Vault' all collide on one key.
    Mirrors the kind of forgiving match Scryfall's fuzzy endpoint gives you,
    but computed locally so lookups never touch the network."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))  # drop accents
    n = n.casefold()
    return "".join(c for c in n if c.isalnum() or c == " ").strip()


def project(card: dict) -> dict:
    """Trim a full Scryfall card to the gameplay fields the app needs.

    IMPORTANT: this is the single source of truth for card shape. mtg_api.py
    imports and reuses it so cached cards and live-API cards are byte-for-byte
    identical to Claude — same keys, same join format on double-faced text."""
    def faces(c):
        return c.get("card_faces") if "card_faces" in c else [c]

    return {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "cmc": card.get("cmc"),
        "type_line": card.get("type_line"),
        "oracle_text": "\n//\n".join(f.get("oracle_text", "") for f in faces(card)),
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty"),
        "colors": card.get("colors") or card.get("color_identity"),
        "color_identity": card.get("color_identity", []),
        "keywords": card.get("keywords", []),
        "legalities": {
            fmt: status
            for fmt, status in card.get("legalities", {}).items()
            if status == "legal"
        },
        "scryfall_uri": card.get("scryfall_uri"),
    }


def fetch_bulk_download_uri(client: httpx.Client) -> tuple[str, str]:
    """Resolve the current oracle_cards download URL + its updated_at stamp.

    The download URL's filename changes daily, so we always ask the API for the
    latest one rather than hardcoding it. This is a single lightweight request."""
    r = client.get(f"{SCRYFALL}/bulk-data/{BULK_TYPE}", timeout=30)
    r.raise_for_status()
    meta = r.json()
    return meta["download_uri"], meta.get("updated_at", "")


def build(out: Path, force: bool = False) -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        download_uri, updated_at = fetch_bulk_download_uri(client)
        print(f"[build] latest {BULK_TYPE} updated_at={updated_at or 'unknown'}")

        # Skip work if our db already reflects this bulk version.
        if out.exists() and not force and _db_stamp(out) == updated_at and updated_at:
            print(f"[build] {out.name} already current — nothing to do "
                  f"(use --force to rebuild).")
            return

        print(f"[build] downloading {download_uri}")
        # The file is large (>150MB raw). Stream it straight to disk instead of
        # holding it all in memory, then parse from the temp file.
        tmp = out.with_suffix(".download.json")
        with client.stream("GET", download_uri, timeout=None) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)

    print("[build] parsing + writing SQLite…")
    cards = json.loads(tmp.read_text(encoding="utf-8"))
    _write_db(out, cards, updated_at)
    tmp.unlink(missing_ok=True)
    print(f"[build] wrote {out} with {len(cards)} cards.")


def _write_db(out: Path, cards: list[dict], updated_at: str) -> None:
    # Build into a temp db then atomically swap, so a running server never
    # reads a half-written file.
    tmp_db = out.with_suffix(".building.db")
    tmp_db.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_db)
    try:
        con.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE cards (
                norm   TEXT NOT NULL,   -- normalized name (lookup key)
                name   TEXT NOT NULL,   -- canonical display name
                data   TEXT NOT NULL    -- JSON: the projected gameplay fields
            );
            CREATE INDEX idx_cards_norm ON cards(norm);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        rows = []
        for c in cards:
            # Skip non-gameplay layouts (tokens, art cards, etc.) — they have no
            # rules text worth ruling on and only pollute fuzzy matches.
            if c.get("layout") in {"token", "double_faced_token", "art_series",
                                    "emblem", "vanguard", "scheme"}:
                continue
            name = c.get("name", "")
            rows.append((normalize_name(name), name, json.dumps(project(c), ensure_ascii=False)))
        con.executemany("INSERT INTO cards (norm, name, data) VALUES (?, ?, ?)", rows)
        con.execute("INSERT INTO meta (key, value) VALUES ('updated_at', ?)", (updated_at,))
        con.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        con.commit()
    finally:
        con.close()
    tmp_db.replace(out)  # atomic on the same filesystem


def _db_stamp(db: Path) -> str:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
            return row[0] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a local Scryfall card cache.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output SQLite path")
    ap.add_argument("--force", action="store_true", help="rebuild even if current")
    args = ap.parse_args(argv)
    try:
        build(args.out, force=args.force)
    except httpx.HTTPError as e:
        print(f"[build] network/HTTP error talking to Scryfall: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
