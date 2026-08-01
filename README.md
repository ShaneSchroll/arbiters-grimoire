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
| `auth.py`           | SQLite-backed login, sessions, and the suspend switch.      |
| `admin.py`          | CLI for managing users and minting password-reset links.   |
| `src/`              | Astro frontend source: pages, layouts, components, scripts, styles. |
| `dist/`             | Astro build output (the HTML/JS/CSS the server actually serves.) |

## Setup

```bash
# 1. Install backend dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Add your ANTHROPIC_API_KEY to a .env file

# 3. Build the rules index from the official .txt (re-run when the rules update)
#    Download it from https://magic.wizards.com/en/rules as rules.txt
python ingest.py rules.txt

# 3a. Build the cards index from Scryfall's Oracle Cards bulk export (gzipped)
#     https://scryfall.com/docs/api/bulk-data — same data build_card_cache.py pulls live
python card_ingest.py oracle-card-data.jsonl.gz

# 3b. Locally, pass --out if CARD_DB_PATH lives only in .env (the CLI doesn't
#     call load_dotenv). On Render it's a real env var, so --out isn't needed.
python card_ingest.py oracle-card-data.jsonl.gz --out /var/data/cards.db

# 4. Build the frontend (Node 22.12+). Re-run after editing anything in src/.
npm install
npm run build

# 5. Start the app — it serves the built site out of dist/
uvicorn server:app --port 8000
```

## Billing ($5/mo subscription + prepaid credits)

Every new account starts on a 7-day trial. After that, users pay a $5/month
Stripe subscription for access plus prepaid usage credits ($5 / $10 / $20
packs) that are deducted (raw Anthropic cost × `CREDIT_MARKUP`) as they use
the Arbiter and Deck Builder.

**Credits are non-refundable**, and an account may hold at most
`MAX_CREDIT_BALANCE_USD` (default $20) at a time — packs that would overshoot
are refused at checkout and greyed out on `/account`. Cancelling always leaves
access running to the end of whatever the user already has (the rest of the
trial, or the period they've paid for), so a balance is never stranded early.

Payments run entirely on Stripe-hosted Checkout; the webhook at
`/api/stripe/webhook` mirrors state into `users.db`. Nothing is enforced until
`BILLING_REQUIRED=1` is set.

## Commands to work with users

```bash
python admin.py list
python admin.py create  alice@example.com [--admin] [--approved]
# Registration is open — accounts arrive usable. `revoke` suspends one,
# `approve` puts it back; `--all` reinstates everyone currently suspended.
python admin.py revoke  alice@example.com
python admin.py approve alice@example.com
python admin.py approve --all
python admin.py make-admin alice@example.com
python admin.py reset   alice@example.com --base-url https://arbitersgrimoire.com
python admin.py delete  alice@example.com --yes

# Monthly spend limits are in credit dollars; users set their own on /account.
python admin.py budget alice@example.com --usd 2.50    # override their limit
python admin.py budget alice@example.com --unlimited   # no cap
python admin.py budget alice@example.com --default     # back to global default
python admin.py usage  alice@example.com               # this month's spend + remaining

# Users self-serve their display name and password from the Account page
# (/account) — the reset command below is for when they're locked out.

# Grants bypass the $20 purchase cap — they're the owner override. A negative
# amount claws back, e.g. to mirror a chargeback settled in Stripe.
python admin.py credits alice@example.com 5            # grant $5 of usage credits
python admin.py credits alice@example.com -2.50        # claw back
python admin.py comp    alice@example.com on           # complimentary subscription
```

Then open **http://localhost:8000**.

The ingester is tuned for the official Comprehensive Rules (chunks by rule
number like `509.2`). If you feed it a prose-style rulebook instead, it
automatically falls back to fixed-size chunks.

## How it works

For each question the backend
    1. Retrieves the most relevant rulebook chunks
    2. Sends them to Claude with a judge-level system prompt
    3. Lets Claude call the Scryfall tool if a card is named
    4. returns the answer plus the rule sources, shown as chips under each reply
