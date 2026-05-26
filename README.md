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

Then open **http://localhost:8000**.

The ingester is tuned for the official Comprehensive Rules (chunks by rule
number like `509.2`). If you feed it a prose-style rulebook instead, it
automatically falls back to fixed-size chunks — either works.

## "Opening it" like an app

The running web app *is* your MTG assistant. To make launching it one click:

- **macOS/Linux:** save a shell script `start.sh` with the `uvicorn` line plus
  a command to open the browser, and put it in your dock / make it executable.
- **Windows:** a `start.bat` with the same two lines.
- The model dropdown in the header lets you switch between **Opus 4.7**
  (deepest rules reasoning — the default) and **Sonnet 4.6** (faster), so it
  feels just like choosing a model.

## How it works

For each question the backend (1) retrieves the 6 most relevant rulebook
chunks, (2) sends them to Claude with a judge-level system prompt, (3) lets
Claude call the Scryfall tool if a card is named, and (4) returns the answer
plus the rule sources, shown as chips under each reply.

The system prompt is split so the static persona is marked for **prompt
caching**, while the per-question rules text stays uncached — standard practice
that trims cost on repeated calls.

## Upgrade ideas

- **Semantic search:** swap BM25 in `retriever.py` for embedding-based
  retrieval if users phrase questions very colloquially.
- **Streaming replies:** switch the `/api/chat` call to the streaming API and
  push tokens to the UI over SSE.
- **More tools:** add rulings search, deck legality checks, or a card-image
  endpoint alongside `lookup_card`.
- **Conversation memory across sessions:** persist `history` to a database.