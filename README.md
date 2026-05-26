# MTG Rules Oracle

A web-based chat assistant that answers Magic: The Gathering rules questions,
grounded in the official rulebook PDF and backed by Claude. It retrieves the
relevant rules for every question, cites rule numbers, and can look up live
card text from Scryfall when a question names a specific card.

## What's inside

| File                | Role                                                        |
|---------------------|-------------------------------------------------------------|
| `ingest.py`         | One-time: turns your PDF into a searchable `index.json`.    |
| `retriever.py`      | BM25 keyword search over those chunks.                      |
| `mtg_api.py`        | Scryfall card-lookup tool exposed to Claude.                |
| `server.py`         | FastAPI backend: retrieval + Claude tool loop.              |
| `static/index.html` | The chat UI (vanilla HTML/JS, no build step).               |
| `auth.py`           | SQLite-backed login, sessions, and approval gate.           |
| `admin.py`          | CLI for approving users and minting password-reset links.   |
| `auth_pages/`       | Login, register, and reset pages.                           |

## Setup

```bash
# 1. Install dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Add your API key

# 3. Index your rulebook PDF (run once; re-run when the PDF changes)
python ingest.py path/to/MagicCompRules.pdf

# 4. Start the app
uvicorn server:app --port 8000
```
## Commands to work with users (for now)

```bash
python admin.py list
python admin.py create  alice@example.com [--admin] [--approved]
python admin.py approve alice@example.com
python admin.py revoke  alice@example.com
python admin.py make-admin alice@example.com
python admin.py reset   alice@example.com [--base-url https://oracle.example.com]
python admin.py delete  alice@example.com [--yes]
```

Then open **http://localhost:8000**.

The ingester is tuned for the official Comprehensive Rules (chunks by rule
number like `509.2`). If you feed it a prose-style rulebook instead, it
automatically falls back to fixed-size chunks — either works.

## How it works

For each question the backend (1) retrieves the 6 most relevant rulebook
chunks, (2) sends them to Claude with a judge-level system prompt, (3) lets
Claude call the Scryfall tool if a card is named, and (4) returns the answer
plus the rule sources, shown as chips under each reply.