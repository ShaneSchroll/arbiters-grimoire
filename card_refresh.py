"""
card_refresh.py — Runs the Scryfall bulk pull in the background for the admin
"Get Latest Cards" button.

Why a background runner and not just calling build() in the request:
  The oracle_cards download is >150MB and parsing ~30k cards takes a while —
  far too long to hold an HTTP request open (proxies like Render would time it
  out). So the endpoint starts the job and returns immediately; the admin UI
  polls get_status() until it finishes.

Why single-flight:
  A threading.Lock guarantees at most one build runs at a time. This is the
  whole point of the button — clicking it twice, or two admins clicking at
  once, must NOT fire two concurrent downloads at Scryfall. Extra clicks just
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
    "updated_at": None,     # Scryfall's bulk-file timestamp now loaded
    "phase": None,          # downloading | parsing | None
    "percent": None,        # 0-100 within the current phase, or None
    "detail": "",           # human one-liner, e.g. "Downloaded 84 / 152 MB"
}

# Don't grab the lock / rebuild the status dict on every byte. The build emits
# progress hundreds-to-thousands of times; we collapse that to a few updates a
# second. This is the only cost the progress feature adds, and it's bounded.
_PROGRESS_MIN_INTERVAL = 0.33  # seconds
_last_emit = 0.0
_PHASE_LABEL = {"downloading": "Downloading", "parsing": "Parsing"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def get_status() -> dict:
    with _lock:
        return dict(_status)


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.0f}"


def _on_progress(phase: str, done: int, total: int) -> None:
    """Throttled bridge from build_card_cache into _status. Called a lot; cheap
    on the common path (a monotonic compare, no lock) and only takes the lock a
    few times per second to publish a snapshot."""
    global _last_emit
    now = time.monotonic()
    at_end = total > 0 and done >= total
    if not at_end and (now - _last_emit) < _PROGRESS_MIN_INTERVAL:
        return
    _last_emit = now

    pct = int(done * 100 / total) if total > 0 else None
    if pct is not None:
        pct = max(0, min(100, pct))
    if phase == "downloading":
        detail = (f"Downloading bulk data — {_mb(done)} / {_mb(total)} MB"
                  if total > 0 else f"Downloading bulk data — {_mb(done)} MB")
    else:
        detail = (f"Parsing & writing cards — {pct}%"
                  if pct is not None else "Parsing & writing cards…")
    _set(phase=phase, percent=pct, detail=detail)


def _run(force: bool) -> None:
    try:
        # build() is itself a no-op if cards.db already matches Scryfall's latest version
        build_card_cache.build(build_card_cache.DEFAULT_OUT, force=force,
                               progress=_on_progress)
        cache = mtg_api.reload_cache()
        _set(
            state="done",
            finished_at=_now(),
            message=f"Loaded {cache.count} cards.",
            count=cache.count,
            updated_at=cache.updated_at,
            phase=None, percent=None, detail="",
        )
    except Exception as e:  # network, parse, disk — surface a short reason
        _set(state="error", finished_at=_now(),
             message=f"{type(e).__name__}: {e}",
             phase=None, percent=None, detail="")
    finally:
        global _thread
        with _lock:
            _thread = None


def start_refresh(force: bool = False) -> dict:
    global _thread, _last_emit
    with _lock:
        if _thread is not None and _thread.is_alive():
            return dict(_status)  # already running — don't start a second
        _last_emit = 0.0
        _status.update(
            state="running", started_at=_now(), finished_at=None,
            message="Contacting Scryfall…",
            phase=None, percent=None, detail="",
        )
        _thread = threading.Thread(target=_run, args=(force,), daemon=True)
        _thread.start()
        return dict(_status)
