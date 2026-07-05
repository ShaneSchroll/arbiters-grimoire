"""
card_ingest.py — Build cards.db from a local Scryfall bulk card file (JSONL).

The card-side companion to ingest.py (which turns the rules .txt into
rules.json/docs.json): this turns a Scryfall bulk *card* export into cards.db,
the exact artifact build_card_cache.py produces from the live bulk download. Use
it to seed or rebuild the card cache from a file you already downloaded — no
network at all.

Input: one JSON card object per line (JSONL), optionally gzip-compressed. Both
the plain `.jsonl` and the `.jsonl.gz` work; the gz is streamed and decompressed
on the fly, so you never need the (much larger) uncompressed copy on disk. Grab a
bulk file from https://scryfall.com/docs/api/bulk-data.

    python card_ingest.py english-card-data.jsonl.gz

Projection, dedup (one row per gameplay-unique card, by oracle_id) and the
on-disk schema are all shared with build_card_cache.py, so a cache built here is
interchangeable with one built from the live download and is read the same way by
card_cache.CardCache. The live "Get Latest Cards" refresh (build_card_cache.py +
card_refresh.py) is unchanged and still pulls from Scryfall's bulk-data endpoint.

Note: this is a one-shot CLI build step (run it, then start/reload the server),
never called on the request hot path.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

import build_card_cache as bcc

DEFAULT_OUT = bcc.DEFAULT_OUT


def _open_stream(path: Path):
    """Open `path` for line iteration, transparently handling gzip.

    Returns (line_iter, tell, total, raw) where tell()/total are the *compressed*
    byte position/size for a .gz — so progress tracks the file you actually read
    off disk — and plain byte position/size otherwise. `raw` is the underlying
    file to close."""
    raw = open(path, "rb")
    total = os.fstat(raw.fileno()).st_size or 1
    magic = raw.read(2)
    raw.seek(0)
    stream = gzip.GzipFile(fileobj=raw) if magic == b"\x1f\x8b" else raw
    return stream, raw.tell, total, raw


def _records(stream):
    """Yield one card dict per non-blank JSONL line."""
    for line in stream:
        line = line.strip()
        if line:
            yield json.loads(line)


def _progress(phase: str, done: int, total: int) -> None:
    pct = int(done * 100 / total) if total else 0
    pct = max(0, min(100, pct))
    bar = "#" * (pct // 4)
    print(f"\r[card_ingest] {phase}: {pct:3d}%  |{bar:<25}|", end="", flush=True)


def build_from_file(src: Path, out: Path) -> int:
    """Build `out` (cards.db) from the local bulk file `src`. Returns card count."""
    stream, tell, total, raw = _open_stream(src)
    try:
        # Version the cache by the source file's mtime, prefixed so it never
        # collides with Scryfall's bulk `updated_at` stamp — a later live refresh
        # will always see a different version and can rebuild over this one.
        updated_at = "local-" + time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(src.stat().st_mtime)
        )
        count = bcc.write_db(
            out, _records(stream), updated_at,
            progress=_progress, tell=tell, total=total,
        )
    finally:
        try:
            stream.close()
        finally:
            if raw is not stream:
                raw.close()
    print()  # terminate the progress line
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build cards.db from a local Scryfall bulk JSONL file."
    )
    ap.add_argument("src", type=Path, help="path to the bulk .jsonl or .jsonl.gz")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output SQLite path")
    args = ap.parse_args(argv)

    if not args.src.exists():
        print(f"File not found: {args.src}", file=sys.stderr)
        return 1

    size_mb = args.src.stat().st_size / 1_048_576
    print(f"[card_ingest] reading {args.src} ({size_mb:.0f} MB)")
    count = build_from_file(args.src, args.out)
    print(f"[card_ingest] wrote {args.out} with {count} unique cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
