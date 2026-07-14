# MTG Arbiters Grimoire

A web-based chat assistant that answers Magic: The Gathering rules questions,
grounded in the official rulebook PDF and backed by Claude. It retrieves the
relevant rules for every question, cites rule numbers, and can look up live
card text from Scryfall when a question names a specific card.

## What's inside

| File                | Role                                                        |
|---------------------|-------------------------------------------------------------|
| `ingest.py`         | Turns the official rules `.txt` into searchable `rules.json` + `docs.json`. |
| `retriever.py`      | BM25 keyword search over those chunks.                      |
| `mtg_api.py`        | Scryfall card-lookup tool exposed to Claude.                |
| `server.py`         | FastAPI backend: retrieval + Claude tool loop. Serves the built frontend from `dist/`. |
| `auth.py`           | SQLite-backed login, sessions, and approval gate.           |
| `admin.py`          | CLI for approving users and minting password-reset links.   |
| `src/`              | Astro frontend source: pages, layouts, components, scripts, styles. |
| `dist/`             | Astro build output (`npm run build`) — the HTML/JS/CSS the server actually serves. |

## Setup

```bash
# 1. Install backend dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Add your ANTHROPIC_API_KEY to a .env file

# 3. Build the rules index from the official .txt (re-run when the rules update)
#    Download it from https://magic.wizards.com/en/rules as rules.txt
python ingest.py rules.txt

# 3a. Build the cards index from the official Scryfall English bulk cards (gzipped for space)
python card_ingest.py english-card-data.jsonl.gz

# 3b. On the live server run with --out since the CLI doesn't call load_dotenv
python card_ingest.py english-card-data.jsonl.gz --out /var/data/cards.db

# 4. Build the frontend (Node 22.12+). Re-run after editing anything in src/.
npm install
npm run build

# 5. Start the app — it serves the built site out of dist/
uvicorn server:app --port 8000
```

## Frontend (Astro)

The UI lives in `src/` and is built with Astro into `dist/`. The Python server
serves the built files directly: each page comes from `dist/<route>/index.html`,
hashed JS/CSS from `dist/_astro/`, and favicons from the `dist/` root. URLs are
unchanged from before the migration — `server.py` maps each route to its built
file.

- **Iterating on the frontend (fast loop):** start the Python server once
  (`uvicorn server:app --port 8000`), then in a second terminal run `npm run dev`.
  Astro serves `src/` with hot reload on http://localhost:4321 and proxies
  `/api/*` and `/docs.json` through to uvicorn (see `astro.config.mjs`), so
  `.astro`/JS/CSS edits appear instantly — no rebuild, no Python restart. Caveat:
  the dev server uses Astro's file routes (`/auth/login`, `/tips`, `/rules`), not
  the Python server's pretty routes (`/login`, `/pages/tips`, `/pages/rules`), so
  sign in at `/auth/login` first to get a session cookie on :4321.
- **Iterating on the frontend (production parity):** edit files under `src/`,
  run `npm run build`, and reload the Python app to serve the real built URLs.
- **Deploying to Render:** `dist/` is committed to the repo, so Render's default
  Python build (`pip install -r requirements.txt`) is all that's needed — no
  Node step at deploy time. **Re-run `npm run build` and commit `dist/` whenever
  you change `src/`,** or the deployed site will be stale. Start command:
  `uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'`
## Commands to work with users

```bash
python admin.py list
python admin.py create  alice@example.com [--admin] [--approved]
python admin.py approve alice@example.com
python admin.py revoke  alice@example.com
python admin.py make-admin alice@example.com
python admin.py reset   alice@example.com --base-url https://arbitersgrimoire.com
python admin.py delete  alice@example.com --yes

python admin.py budget alice@example.com --usd 2.50    # custom daily cap
python admin.py budget alice@example.com --unlimited   # no cap
python admin.py budget alice@example.com --default     # back to global default
python admin.py usage  alice@example.com               # today's spend + remaining
```

Then open **http://localhost:8000**.

The ingester is tuned for the official Comprehensive Rules (chunks by rule
number like `509.2`). If you feed it a prose-style rulebook instead, it
automatically falls back to fixed-size chunks.

## How it works

For each question the backend
    1. Retrieves the 6 most relevant rulebook chunks
    2. Sends them to Claude with a judge-level system prompt
    3. Lets Claude call the Scryfall tool if a card is named
    4. returns the answer plus the rule sources, shown as chips under each reply
