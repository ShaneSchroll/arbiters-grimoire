"""
card_cache.py — Read-only loader over cards.db (built by build_card_cache.py).

This is the cards-side analogue of retriever.py: instantiate once at server
startup (`cards = CardCache()`) and share the single instance across every
request and every page. Both the Rules Oracle's lookup_card tool and the Deck
Builder hit this same object, so "both pages share the cached card data" is
satisfied at the source — one process-wide cache, zero network on the hot path.

Lookups are exact-by-normalized-name first, then a cheap local fuzzy fallback.
A true miss returns None so the caller can decide whether to fall back to the
live Scryfall API (see mtg_api.lookup_card) for a card too new to be in the
last bulk build.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from build_card_cache import normalize_name  # single source of truth for keys


class CardCache:
    def __init__(self, db_path: str | None = None):
        # Mirror build_card_cache.DEFAULT_OUT: explicit arg, then CARD_DB_PATH
        # (persistent disk on the server), then next to the code for local dev.
        path = Path(db_path or os.getenv("CARD_DB_PATH") or Path(__file__).parent / "cards.db")
        if not path.exists():
            raise FileNotFoundError(
                "cards.db not found. Run `python build_card_cache.py` first."
            )
        # check_same_thread=False: FastAPI serves requests on a threadpool and
        # this connection is read-only, so sharing it is safe. mode=ro makes
        # that guarantee explicit and lets the build step swap the file freely.
        self._con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                    check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        row = self._con.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
        self.updated_at = row["value"] if row else ""
        (self.count,) = self._con.execute("SELECT COUNT(*) FROM cards").fetchone()

    def get(self, name: str) -> dict | None:
        """Return the projected card dict for `name`, or None if not cached.

        Match order: exact normalized name → unique prefix → unique substring.
        The prefix/substring steps cover light typos and partial names the way
        Scryfall's fuzzy endpoint does, without leaving the box."""
        norm = normalize_name(name)
        if not norm:
            return None

        row = self._con.execute(
            "SELECT data FROM cards WHERE norm = ? LIMIT 1", (norm,)
        ).fetchone()
        if row:
            return json.loads(row["data"])

        # Unique prefix match (e.g. "lightning bo" -> "Lightning Bolt").
        rows = self._con.execute(
            "SELECT data FROM cards WHERE norm LIKE ? LIMIT 2", (norm + "%",)
        ).fetchall()
        if len(rows) == 1:
            return json.loads(rows[0]["data"])

        # Unique substring match (e.g. "bolt" alone is ambiguous -> skip;
        # "snapcaster" -> "Snapcaster Mage").
        rows = self._con.execute(
            "SELECT data FROM cards WHERE norm LIKE ? LIMIT 2", ("%" + norm + "%",)
        ).fetchall()
        if len(rows) == 1:
            return json.loads(rows[0]["data"])

        return None

    def autocomplete(self, prefix: str, limit: int = 10) -> list[str]:
        """Names starting with `prefix` — handy for a deck-builder name field.
        Cheap enough to call per keystroke; backs the optional /api/card/suggest
        endpoint."""
        norm = normalize_name(prefix)
        if not norm:
            return []
        rows = self._con.execute(
            "SELECT name FROM cards WHERE norm LIKE ? ORDER BY length(name) LIMIT ?",
            (norm + "%", limit),
        ).fetchall()
        return [r["name"] for r in rows]
