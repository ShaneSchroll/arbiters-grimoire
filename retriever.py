"""
retriever.py — Keyword (BM25) search over the chunks produced by ingest.py.

BM25 is a strong fit for a rules document: Magic terminology is precise
("first strike", "state-based actions", "the stack"), so exact-term matching
retrieves the right rules reliably without needing an embedding model or any
extra API calls. To upgrade to semantic search later, swap `Retriever.search`
for an embedding-based lookup — the interface can stay the same.
"""

import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+[a-z]?)?")


def tokenize(text: str):
    """Lowercase tokens; keeps rule numbers like '509.2a' intact."""
    return _TOKEN.findall(text.lower())


class Retriever:
    def __init__(self, index_path: str = None):
        path = Path(index_path or Path(__file__).parent / "index.json")
        if not path.exists():
            raise FileNotFoundError(
                "index.json not found. Run `python ingest.py <your.pdf>` first."
            )
        # encoding="utf-8" matches how ingest.py writes the file; without it
        # Windows would fail to read characters like the minus sign (U+2212).
        self.chunks = json.loads(path.read_text(encoding="utf-8"))
        self._bm25 = BM25Okapi([tokenize(c["text"]) for c in self.chunks])

    def search(self, query: str, k: int = 6):
        """Return the k best-matching chunks for the query (best effort)."""
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(
            range(len(self.chunks)), key=lambda i: scores[i], reverse=True
        )
        return [self.chunks[i] for i in ranked[:k]]
