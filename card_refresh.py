"""
card_refresh.py — Runs the Scryfall bulk pull in the background for the admin
"Get Latest Cards" button.

The oracle_cards download is >150MB and parsing ~30k cards takes a while —
  far too long to hold an HTTP request open (proxies like Render would time it
  out). So the endpoint starts the job and returns immediately; the admin UI
  polls get_status() until it finishes.

A threading.Lock guarantees at most one build runs at a time. Extra clicks just
  return the in-progress status.
"""

from __future__ import annotations

import threading
import time

import build_card_cache
import mtg_api

_lock = threading.Lock()
_thread: threading.Thread | None = None

# Snapshot of the most recent / current run. Guarded by _lock for writes;
# reads return a shallow copy so callers never see a half-updated dict.
_status: dict = {
    "state": "idle",        # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "message": "",
    "count": None,          # cards in the cache after a successful build
    "updated_at": None,     # Scryfall's bulk-file timestamp
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def get_status() -> dict:
    with _lock:
        return dict(_status)


def _run(force: bool) -> None:
    try:
        # build() is itself a no-op if cards.db already matches Scryfall's latest version
        build_card_cache.build(build_card_cache.DEFAULT_OUT, force=force)
        cache = mtg_api.reload_cache()
        _set(
            state="done",
            finished_at=_now(),
            message=f"Loaded {cache.count} cards.",
            count=cache.count,
            updated_at=cache.updated_at,
        )
    except Exception as e:
        _set(state="error", finished_at=_now(),
             message=f"{type(e).__name__}: {e}")
    finally:
        global _thread
        with _lock:
            _thread = None


def start_refresh(force: bool = False) -> dict:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return dict(_status)
        _status.update(
            state="running", started_at=_now(), finished_at=None,
            message="Contacting Scryfall…",
        )
        _thread = threading.Thread(target=_run, args=(force,), daemon=True)
        _thread.start()
        return dict(_status)
