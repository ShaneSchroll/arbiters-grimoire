"""
mtg_api.py — Card lookups for the Rules Oracle and Deck Builder.

Now cache-first: lookup_card consults the local CardCache (cards.db) and only
touches the live Scryfall API on a miss — e.g. a card printed after the last
bulk build. Misses are memoized so a given name hits the network at most once
per process. This keeps the rules tool working for brand-new cards while
respecting Scryfall's "don't crawl us" guidance for everything already cached.

The CardCache singleton here is the shared card store: server.py and
deckbuilder.py both call get_cache(), so every page reads one in-process cache.
"""

from __future__ import annotations

import httpx

from build_card_cache import project  # identical shape for cached + live cards
from card_cache import CardCache

SCRYFALL = "https://api.scryfall.com"
_HEADERS = {"User-Agent": "mtg-rules-assistant/1.0", "Accept": "application/json"}

_cache: CardCache | None = None
_live_misses: dict[str, dict] = {}  # memoized live-API results for cache misses
# Hard cap so a flood of distinct junk names can't grow the memo without bound.
# On overflow the memo is simply dropped and rebuilt — it's only a cache.
_MAX_LIVE_MISSES = 10_000


def _remember(key: str, result: dict) -> dict:
    if len(_live_misses) >= _MAX_LIVE_MISSES:
        _live_misses.clear()
    _live_misses[key] = result
    return result


def get_cache() -> CardCache:
    """Lazily open the shared, process-wide card cache. Both pages call this,
    so they read the same instance. Raises if cards.db hasn't been built yet."""
    global _cache
    if _cache is None:
        _cache = CardCache()
    return _cache


def reload_cache() -> CardCache:
    """Re-open cards.db and swap in a fresh CardCache. Called after an admin
    'Get Latest Cards' rebuild so new cards go live without a server restart.

    Reassigning the module reference is atomic, so in-flight requests that
    already hold the old instance keep reading it safely; new requests pick up
    the new one. The old connection is closed by GC once nothing references it.
    Also clears memoized live misses, since a name that missed before may now
    be in the cache."""
    global _cache
    _cache = CardCache()
    _live_misses.clear()
    return _cache


def _live_lookup(name: str) -> dict:
    """Fall back to Scryfall's fuzzy named endpoint for a single uncached card.
    Used sparingly (cache misses only) and memoized, so it never becomes a crawl."""
    key = name.strip().casefold()
    if key in _live_misses:
        return _live_misses[key]
    try:
        r = httpx.get(
            f"{SCRYFALL}/cards/named",
            params={"fuzzy": name},
            timeout=10,
            headers=_HEADERS,
        )
    except httpx.RequestError as e:
        return {"error": f"network error contacting Scryfall: {e}"}

    if r.status_code == 404:
        # Memoize the definitive no-match too: without this, every repeat of a
        # junk name is a fresh outbound request — an easy way for one user to
        # crawl Scryfall through us. Cleared on cache rebuild like the hits.
        return _remember(key, {"error": f"no card found matching '{name}'"})
    if r.status_code != 200:
        # Transient upstream trouble (5xx, rate limit) — do NOT memoize, so the
        # next attempt can succeed once Scryfall recovers.
        return {"error": f"Scryfall returned HTTP {r.status_code}"}

    return _remember(key, project(r.json()))


def lookup_card(name: str) -> dict:
    """Fuzzy-match a card by name. Cache first, live API only on a miss.
    Returns {"error": ...} when nothing matches, so Claude reacts gracefully."""
    try:
        hit = get_cache().get(name)
    except FileNotFoundError:
        hit = None  # cache not built yet — degrade to live lookups
    if hit:
        return hit
    return _live_lookup(name)


# Tool schema advertised to Claude — unchanged; the speedup is invisible to it.
CARD_TOOL = {
    "name": "lookup_card",
    "description": (
        "Look up an official Magic: The Gathering card by name to get its "
        "current Oracle text, mana cost, type line, power/toughness, and "
        "format legality. Use this whenever a question references a specific "
        "card and the exact wording matters for the ruling."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Card name (approximate spelling is fine).",
            }
        },
        "required": ["name"],
    },
}
