"""
auth.py - Authentication for the MTG Rules Oracle.
The router is mounted by server.py. The require_user dependency is what gates
the chat endpoint.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    """Environment lookup that treats a blank value as unset.

    os.getenv() only falls back when the key is ABSENT, and a key set to the
    empty string is very easy to produce by accident: `FOO=` in an env file
    (python-dotenv puts it in os.environ as "") and a Render dashboard row with
    the value field left empty both do it. Without this, `CREDIT_MARKUP=` would
    reach float("") and take the whole app down at import with a ValueError
    naming neither the variable nor the file. Blank now means "use the default",
    which is what anyone writing it meant.
    """
    return (os.getenv(name) or "").strip() or default


DB_PATH = Path(_env("AUTH_DB_PATH", str(BASE_DIR / "users.db")))

SESSION_COOKIE = "session"
SESSION_TTL = timedelta(days=30)
RESET_TOKEN_TTL = timedelta(hours=24)

# Failed-login limits. Two layers: per (ip, email) to stop hammering one
# account, and per ip to blunt password-spraying across many accounts.
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILS = 3
LOGIN_IP_MAX_FAILS = 10

# New-account attempts per source IP. Argon2 is deliberately expensive and each
# new account writes a row, so unbounded registration is a CPU + storage DoS.
REGISTER_WINDOW = timedelta(minutes=15)
REGISTER_MAX = 5

# Chat requests per user
CHAT_WINDOW = timedelta(minutes=1)
CHAT_MAX = 5

# Card-preview lookups per user (/api/card). Generous — hovers are cheap and
# cache-first — but bounds how hard one user can drive live Scryfall fallbacks.
CARD_WINDOW = timedelta(minutes=1)
CARD_MAX = 30

# Self-service password-reset links per user. Each call verifies the current
# password and writes a token row, so this bounds both online guessing of that
# password and unbounded token creation.
PWLINK_WINDOW = timedelta(hours=1)
PWLINK_MAX = 5

# Trusted reverse proxies in front of the app, counted from the connection
# inward: Render alone = 1; Cloudflare -> Render = 2. The env var MUST match
# the real chain: too low and per-IP rate limits key on a proxy's (shared) IP;
# too high and clients can spoof their IP via X-Forwarded-For.
TRUSTED_PROXY_HOPS = int(_env("TRUSTED_PROXY_HOPS", "1"))

# ---------- spend accounting ----------
# Monthly spend cap for an account that has never bought credits. Denominated
# in CREDIT dollars (what the balance actually drops by), not raw Anthropic
# cost - see usage_month_credit_micros. Buying credits replaces this with the
# user's own limit, defaulting to their balance (ensure_monthly_limit_default).
DEFAULT_MONTHLY_BUDGET_MICROS = int(
    float(_env("MONTHLY_BUDGET_USD", "5.00")) * 1_000_000
)

PRICING = {
    "claude-opus-4-8": {
        "input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50,
    },
}
# Fallback for an unrecognized model: the most expensive rate in each column,
# so an unknown model can never be under-billed past the monthly budget.
_FALLBACK_RATE = {
    field: max(p[field] for p in PRICING.values())
    for field in ("input", "output", "cache_write", "cache_read")
}

# ---------- billing (subscription + prepaid credits) ----------
# Master switch. When off (the default) the subscription/credits gate is not
# enforced and usage does not deduct credits, so this code can be deployed,
# Stripe configured, and credits granted/tested before anyone is locked out.
# Flip BILLING_REQUIRED=1 only after the SETUP-BILLING.md checklist is done.
BILLING_REQUIRED = _env("BILLING_REQUIRED", "0").lower() in ("1", "true", "yes")

# Multiplier applied to the raw Anthropic cost when deducting credits. Covers
# Stripe's fee (2.9% + $0.30), the retrieval context the user never sees, and
# margin. The usage_ledger keeps recording RAW cost; only the credit deduction
# is marked up, so the markup can be tuned without rewriting history.
CREDIT_MARKUP = float(_env("CREDIT_MARKUP", "1.4"))

# Hard ceiling on the credits one account may hold at once. Credits are
# non-refundable, so this bounds what any single change of heart (or
# chargeback) can be worth. Enforced at checkout - billing.py refuses a pack
# that would overshoot - and surfaced as disabled pack buttons on /account.
MAX_CREDIT_BALANCE_USD = float(_env("MAX_CREDIT_BALANCE_USD", "20"))
MAX_CREDIT_BALANCE_MICROS = int(MAX_CREDIT_BALANCE_USD * 1_000_000)

# Bounds on the monthly limit a user may choose for themselves. The floor keeps
# a stray "0" from silently refusing every request; the ceiling is the most
# credits they could hold anyway, so anything higher is the same as no limit.
MIN_MONTHLY_LIMIT_MICROS = 250_000  # $0.25
MAX_MONTHLY_LIMIT_MICROS = MAX_CREDIT_BALANCE_MICROS

# Subscription statuses that count as "may use the app". 'active'/'trialing'
# come from Stripe; 'comp' is a local, admin-granted status Stripe never sets
# (complimentary access - e.g. the owner and testers).
_SUB_OK_STATUSES = ("active", "trialing", "comp")

# Grace window past the paid-through date, so a briefly-late renewal webhook
# doesn't lock a paying user out the second their period ticks over. Applies
# to PAID subscriptions only - a trial gets exactly its TRIAL_DAYS, and a
# cancellation ends exactly when it says it does.
SUB_GRACE = timedelta(days=3)

# Every new account starts on a free trial of this length, granted at
# registration. It opens the subscription half of the gate only - usage still
# needs purchased credits - so nobody spends API dollars for free, and
# cancelling before it runs out simply means never being charged.
TRIAL_DAYS = int(_env("TRIAL_DAYS", "7"))

PASSWORD_MIN_LEN = 12
# Upper bound applied at the API boundary. Without one, uvicorn accepts
# arbitrarily large bodies and Argon2 would grind through a multi-megabyte
# "password" — free CPU burn for an attacker. 128 chars is far beyond any
# real passphrase.
PASSWORD_MAX_LEN = 128

# Optional display name shown in the sidebar. Kept short; falls back to the
# email when unset. Trimmed and capped so a long value can't bloat the row.
MAX_NAME_LEN = 60


def _clean_name(name: Optional[str]) -> Optional[str]:
    """Normalize an optional display name: trim, cap length, and treat blank as
    unset (stored as NULL so the UI can fall back to the email)."""
    if not name:
        return None
    name = name.strip()[:MAX_NAME_LEN]
    return name or None


_ph = PasswordHasher()
# Used to keep the argon2 verify cost constant when the email doesn't exist,
# so an attacker can't tell registered emails from unregistered ones by timing.
_DUMMY_HASH = _ph.hash("not-a-real-password-only-for-timing-safety")

# ---------- low-level helpers ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normalize_email(email: str) -> str:
    info = validate_email(email, check_deliverability=False)
    return info.normalized.lower()


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """Wrap a read-then-write so it is atomic against other processes.

    The connection is in autocommit, so each statement would otherwise commit on
    its own and two workers could both read "4 events" before either inserted
    its fifth. BEGIN IMMEDIATE takes SQLite's write lock up front, which
    serializes the whole read-decide-write against every other worker on the
    same file. Waiting for that lock is bounded by sqlite3's busy timeout (5s
    by default) — far beyond anything these millisecond transactions need.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db() -> None:
    with _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              email         TEXT    NOT NULL UNIQUE,
              password_hash TEXT    NOT NULL,
              -- Registration is open: accounts start usable and `approved` is
              -- a moderation switch (admin.py revoke / approve) rather than a
              -- gate every signup has to wait behind.
              approved      INTEGER NOT NULL DEFAULT 1,
              is_admin      INTEGER NOT NULL DEFAULT 0,
              created_at    TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT    PRIMARY KEY,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT    NOT NULL,
              expires_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS reset_tokens (
              token_hash TEXT    PRIMARY KEY,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT    NOT NULL,
              expires_at TEXT    NOT NULL,
              used_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_ledger (
              id                 INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              model              TEXT    NOT NULL,
              input_tokens       INTEGER NOT NULL DEFAULT 0,
              output_tokens      INTEGER NOT NULL DEFAULT 0,
              cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
              cost_micros        INTEGER NOT NULL DEFAULT 0,
              created_at         TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_user_time
              ON usage_ledger(user_id, created_at);
            CREATE TABLE IF NOT EXISTS credits_ledger (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              amount_micros INTEGER NOT NULL,  -- + purchase/grant, - usage
              kind          TEXT    NOT NULL,  -- purchase | grant | usage
              stripe_ref    TEXT    UNIQUE,    -- checkout session id; dedupes webhook retries
              note          TEXT,
              created_at    TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_credits_user
              ON credits_ledger(user_id, created_at);
            CREATE TABLE IF NOT EXISTS conversations (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title      TEXT    NOT NULL,
              format     TEXT    NOT NULL DEFAULT 'Commander',
              messages   TEXT    NOT NULL,   -- JSON: [{role, content}] display turns
              created_at TEXT    NOT NULL,
              updated_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user
              ON conversations(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS rate_limits (
              bucket     TEXT NOT NULL,  -- which limiter: 'chat', 'login_ip', …
              key        TEXT NOT NULL,  -- what it counts: user id, IP, ip+email
              -- Unix seconds, not the ISO text the tables above use: these
              -- windows are sub-minute and the limiter is on the request hot
              -- path, so a float that compares and adds directly beats
              -- formatting and parsing a timestamp on every call.
              expires_at REAL NOT NULL   -- when this event leaves its window
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limits
              ON rate_limits(bucket, key, expires_at);
            """
        )
        _migrate(db)
    _cleanup_expired()


def _migrate(db: sqlite3.Connection) -> None:
    """Apply additive schema changes to an already-populated database. Only
    adds columns that are missing, so it is safe to run on every startup and
    never touches existing rows. (CREATE TABLE IF NOT EXISTS won't add a new
    column to a table that already exists, hence this guarded ALTER.)"""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "name" not in cols:
        # Optional display name captured at registration. NULL means "unset",
        # in which case the UI falls back to the email's local-part.
        db.execute("ALTER TABLE users ADD COLUMN name TEXT")
    if "monthly_budget_micros" not in cols:
        # The monthly spend limit, in credit dollars. Nullable: NULL means "use
        # DEFAULT_MONTHLY_BUDGET_MICROS" and is the state of an account that
        # has never bought credits. A negative value means unlimited (admin
        # only). Anything else is the limit the user picked, or the balance
        # snapshot ensure_monthly_limit_default wrote at their first purchase.
        db.execute("ALTER TABLE users ADD COLUMN monthly_budget_micros INTEGER")
        if "daily_budget_micros" in cols:
            # Carry the old per-day limits over verbatim. They were seeded from
            # the balance and bounded by the same [MIN, MAX] range as the new
            # column, so every stored value is still a valid monthly figure -
            # just a stricter one, which is the safe direction to land in.
            db.execute(
                "UPDATE users SET monthly_budget_micros = daily_budget_micros"
            )
    # Columns that no longer back anything: the pre-monthly spend limit, and
    # per-user Opus gating (every user gets the same model now). Dropped rather
    # than left dangling so the row can't drift out of sync with the code that
    # reads it. DROP COLUMN needs SQLite 3.35+; on anything older the columns
    # simply stay put, unread and harmless.
    for dead in ("daily_budget_micros", "opus_allowed"):
        if dead in cols:
            try:
                db.execute(f"ALTER TABLE users DROP COLUMN {dead}")
            except sqlite3.OperationalError:
                pass
    # NOTE: `approved` gained a DEFAULT of 1 (registration is open). SQLite
    # can't alter a column default in place, but nothing relies on it - every
    # insert passes `approved` explicitly - so existing databases need no
    # rewrite. Accounts already sitting at approved=0 stay that way; approve
    # them with `python admin.py approve <email>` (or `approve --all`).
    if "stripe_customer_id" not in cols:
        # The Stripe Customer this user maps to. Created lazily on their first
        # checkout; webhooks resolve users through it. Uniqueness is enforced
        # by an index (ALTER ADD COLUMN can't carry a UNIQUE constraint).
        db.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer "
            "ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
        )
    if "subscription_status" not in cols:
        # Mirror of the Stripe subscription status ('active', 'trialing',
        # 'past_due', 'canceled', ...) or the local 'comp'. NULL = never
        # subscribed. Kept current by webhooks + /api/billing/refresh.
        db.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT")
    if "subscription_period_end" not in cols:
        # ISO timestamp the subscription is paid through. The gate allows
        # SUB_GRACE past it so a late renewal webhook doesn't lock users out.
        db.execute("ALTER TABLE users ADD COLUMN subscription_period_end TEXT")
    if "trial_ends_at" not in cols:
        # End of the free trial granted at registration. NULL on accounts that
        # predate trials - they simply never had one, and fall through to the
        # normal subscription rules.
        db.execute("ALTER TABLE users ADD COLUMN trial_ends_at TEXT")
    if "canceled_at" not in cols:
        # When the user asked to cancel. NULL = not cancelled. Set locally even
        # when there is no Stripe subscription (e.g. cancelling a free trial).
        db.execute("ALTER TABLE users ADD COLUMN canceled_at TEXT")
    if "access_ends_at" not in cols:
        # When a cancellation actually cuts access off: the end of whatever the
        # user already has - the rest of the trial, or the period they've paid
        # for. Credits are non-refundable, so access is never cut early.
        db.execute("ALTER TABLE users ADD COLUMN access_ends_at TEXT")


def _cleanup_expired() -> None:
    """Delete rows that can no longer authenticate or count against anything, so
    sessions, reset_tokens and rate_limits don't grow without bound on the
    persistent disk."""
    now_iso = _now().isoformat()
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        db.execute(
            "DELETE FROM reset_tokens WHERE expires_at <= ? OR used_at IS NOT NULL",
            (now_iso,),
        )
        # Every rate_limits row carries its own expiry, so one statement sweeps
        # every bucket — including buckets no limiter uses any more. Live
        # buckets also prune themselves on write; this catches the ones that
        # went quiet, and whatever went stale while the app was down.
        db.execute("DELETE FROM rate_limits WHERE expires_at <= ?", (time.time(),))


# ---------- rate limiting ----------
# Shared, not process-local: the counters live in users.db, so N uvicorn workers
# enforce ONE limit instead of N of them. They also survive a restart, so a
# redeploy no longer hands an attacker mid-spray a fresh budget.

# Hard ceiling on the rows one bucket may hold at once. Expired rows are pruned
# on every write, so this only bites during a flood of distinct keys from many
# IPs at once; it then drops the closest-to-expiring rows first, which keeps the
# disk bounded at the cost of forgiving the oldest offenders slightly early.
_MAX_BUCKET_ROWS = 50_000


class _RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string, stored in the
    `rate_limits` table: one row per event, holding the moment that event stops
    counting.

    `record` and `hit` run inside `_write_tx`, which is what makes
    count-then-insert atomic between workers. `blocked` is a single SELECT and
    needs no transaction.

    A SQLite error propagates rather than failing open. Anything that reaches a
    limiter has already read users.db to resolve the session, so a database that
    can't be read was going to fail the request either way — and a limiter that
    silently stopped counting is exactly the failure this table exists to
    prevent.
    """

    def __init__(self, bucket: str, max_events: int, window: timedelta) -> None:
        self._bucket = bucket
        self._max = max_events
        self._window = window.total_seconds()

    def _live(self, db: sqlite3.Connection, key: str, now: float) -> int:
        """Events for `key` still inside the window."""
        return db.execute(
            "SELECT COUNT(*) FROM rate_limits "
            "WHERE bucket = ? AND key = ? AND expires_at > ?",
            (self._bucket, key, now),
        ).fetchone()[0]

    def _add(self, db: sqlite3.Connection, key: str, now: float) -> None:
        # Prune the bucket's dead rows on the way in. One delete per insert
        # amortized, so the table settles at roughly the number of events
        # actually inside the window rather than growing with total traffic.
        db.execute(
            "DELETE FROM rate_limits WHERE bucket = ? AND expires_at <= ?",
            (self._bucket, now),
        )
        db.execute(
            "INSERT INTO rate_limits (bucket, key, expires_at) VALUES (?, ?, ?)",
            (self._bucket, key, now + self._window),
        )
        rows = db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE bucket = ?", (self._bucket,)
        ).fetchone()[0]
        if rows > _MAX_BUCKET_ROWS:
            db.execute(
                "DELETE FROM rate_limits WHERE rowid IN ("
                "  SELECT rowid FROM rate_limits WHERE bucket = ?"
                "  ORDER BY expires_at LIMIT ?)",
                (self._bucket, rows - _MAX_BUCKET_ROWS),
            )

    def blocked(self, key: str) -> bool:
        """True if the key is already at/over the limit (no event recorded)."""
        with _db() as db:
            return self._live(db, key, time.time()) >= self._max

    def record(self, key: str) -> None:
        """Record one event against the key."""
        with _db() as db, _write_tx(db):
            self._add(db, key, time.time())

    def hit(self, key: str) -> bool:
        """Record one event and return True if the key is now over the limit."""
        with _db() as db, _write_tx(db):
            now = time.time()
            over = self._live(db, key, now) + 1 > self._max
            self._add(db, key, now)
            return over

    def clear(self, key: str) -> None:
        with _db() as db:
            db.execute(
                "DELETE FROM rate_limits WHERE bucket = ? AND key = ?",
                (self._bucket, key),
            )


# Bucket names are the stored identity of each limiter: renaming one resets it
# (its old rows simply age out), so keep them stable.
_login_fail = _RateLimiter("login_fail", LOGIN_MAX_FAILS, LOGIN_WINDOW)
_login_ip = _RateLimiter("login_ip", LOGIN_IP_MAX_FAILS, LOGIN_WINDOW)
_register_limit = _RateLimiter("register", REGISTER_MAX, REGISTER_WINDOW)
_chat_limit = _RateLimiter("chat", CHAT_MAX, CHAT_WINDOW)
_card_limit = _RateLimiter("card", CARD_MAX, CARD_WINDOW)
_pwlink_limit = _RateLimiter("pwlink", PWLINK_MAX, PWLINK_WINDOW)


def chat_rate_limited(user_id: int) -> bool:
    """Record a chat request for this user; True if they are now over CHAT_MAX."""
    return _chat_limit.hit(str(user_id))


def card_rate_limited(user_id: int) -> bool:
    """Record a card lookup for this user; True if they are now over CARD_MAX."""
    return _card_limit.hit(str(user_id))


# ---------- spend accounting ----------

def _cost_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> int:
    """Cost of one API call in integer micro-dollars. A token priced at $X per
    million tokens costs exactly X micro-dollars, so the rate doubles as the
    per-token micro-dollar price. Unknown models fall back to the priciest
    rates so we never under-bill."""
    r = PRICING.get(model, _FALLBACK_RATE)
    if r is _FALLBACK_RATE or "input" not in r:
        r = _FALLBACK_RATE
    total = (
        input_tokens * r["input"]
        + output_tokens * r["output"]
        + cache_write_tokens * r["cache_write"]
        + cache_read_tokens * r["cache_read"]
    )
    return round(total)


def record_usage(
    user_id: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int:
    """Append one row to the usage ledger. Returns the cost in micro-dollars.
    Costs are locked in at record time from the PRICING then in effect, so the
    ledger is an immutable record even if rates change later."""
    cost = _cost_micros(
        model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens
    )
    now_iso = _now().isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO usage_ledger (user_id, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_micros, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, model, input_tokens, output_tokens,
                cache_write_tokens, cache_read_tokens, cost, now_iso,
            ),
        )
        # Deduct prepaid credits at the marked-up rate. Only while billing is
        # enforced, so pre-launch usage never drives balances negative before
        # anyone has had a chance to buy credits.
        if BILLING_REQUIRED and cost > 0:
            db.execute(
                "INSERT INTO credits_ledger (user_id, amount_micros, kind, note, "
                "created_at) VALUES (?, ?, 'usage', ?, ?)",
                (user_id, -int(round(cost * CREDIT_MARKUP)), model, now_iso),
            )
    return cost


def _utc_month_start_iso() -> str:
    """00:00 UTC on the 1st of the current calendar month - the instant every
    spend window resets. Calendar months, not rolling 30-day windows, so the
    reset date a user is told is a real date they can put in a diary."""
    return _now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def _next_month_start_iso() -> str:
    """00:00 UTC on the 1st of NEXT month - when the current window rolls over.
    A month is long enough that "it resets eventually" isn't good enough: the
    account page shows this date so someone who has hit their cap knows exactly
    how long they're waiting."""
    start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # 32 days past the 1st always lands somewhere inside the following month
    # (months run 28-31 days), so snapping back to day 1 gives its start with
    # no calendar arithmetic and no December wrap-around to special-case.
    return (start + timedelta(days=32)).replace(day=1).isoformat()


def usage_month_micros(user_id: int) -> int:
    """Total spend (micro-dollars) for this user since the 1st of the month."""
    with _db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(cost_micros), 0) AS total FROM usage_ledger "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, _utc_month_start_iso()),
        ).fetchone()
    return int(row["total"])


def usage_month_credit_micros(user_id: int) -> int:
    """This month's spend in CREDIT dollars: the raw cost marked up exactly the
    way record_usage deducts it. The monthly limit and every user-facing spend
    readout are denominated this way so they line up with the balance the user
    watches drop - a $5 limit means $5 off the balance, not $5 of raw cost
    (which would be $7 of credits). Derived from the raw ledger rather than
    summed from credits_ledger so the cap still works with BILLING_REQUIRED
    off, when nothing is being deducted yet."""
    return int(round(usage_month_micros(user_id) * CREDIT_MARKUP))


def _budget_from_row(row) -> int:
    """Monthly limit (credit micro-dollars) from an already-loaded user row.
    NULL -> the default; negative -> unlimited (a sentinel the callers treat as
    no cap)."""
    if row is None:
        return DEFAULT_MONTHLY_BUDGET_MICROS
    override = row["monthly_budget_micros"]
    return DEFAULT_MONTHLY_BUDGET_MICROS if override is None else int(override)


def _budget_for(user_id: int) -> int:
    """This user's monthly limit in credit micro-dollars."""
    with _db() as db:
        row = db.execute(
            "SELECT monthly_budget_micros FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _budget_from_row(row)


def monthly_budget_exceeded(user_id: int) -> bool:
    """True if the user has already met or passed their monthly limit. Checked
    BEFORE a request starts; since token cost isn't known until generation
    finishes, the request that crosses the line is allowed to complete and the
    next one is refused. Per-request overshoot is bounded by max_tokens and the
    tool-loop cap in server.py."""
    budget = _budget_for(user_id)
    if budget < 0:
        return False  # unlimited
    return usage_month_credit_micros(user_id) >= budget


def set_monthly_budget(email: str, micros: Optional[int]) -> bool:
    """Set a per-user monthly limit override by email. None clears it (back to
    default); a negative value means unlimited. Called by admin.py, which is
    the only path allowed to clear it or lift the cap entirely."""
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET monthly_budget_micros = ? WHERE email = ?",
            (micros, _normalize_email(email)),
        )
        return cur.rowcount > 0


def set_user_monthly_limit(user_id: int, micros: int) -> None:
    """The self-service setter behind the account page. Always stores a
    concrete number inside [MIN_MONTHLY_LIMIT_MICROS, MAX_MONTHLY_LIMIT_MICROS]
    - users can't clear the limit or make it unlimited, only admins can."""
    micros = max(MIN_MONTHLY_LIMIT_MICROS, min(int(micros), MAX_MONTHLY_LIMIT_MICROS))
    with _db() as db:
        db.execute(
            "UPDATE users SET monthly_budget_micros = ? WHERE id = ?",
            (micros, user_id),
        )


def ensure_monthly_limit_default(user_id: int) -> None:
    """Give a user their first monthly limit the moment they first hold
    credits: their whole balance, i.e. "I bought $10, I can spend $10 this
    month".

    Only ever fills a NULL, which is the point - topping up later must NOT
    raise (or reset) a limit the user has since chosen, and an admin override
    or an 'unlimited' setting is likewise left alone.
    """
    with _db() as db:
        row = db.execute(
            "SELECT monthly_budget_micros FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or row["monthly_budget_micros"] is not None:
            return
        balance = int(db.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) AS total FROM credits_ledger "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()["total"])
        if balance <= 0:
            return
        db.execute(
            "UPDATE users SET monthly_budget_micros = ? "
            "WHERE id = ? AND monthly_budget_micros IS NULL",
            (max(MIN_MONTHLY_LIMIT_MICROS, min(balance, MAX_MONTHLY_LIMIT_MICROS)), user_id),
        )


def monthly_limit_view(user_row) -> dict:
    """The monthly-limit block the account page renders and edits. All amounts
    are credit dollars (see usage_month_credit_micros)."""
    budget = _budget_from_row(user_row)
    spent = usage_month_credit_micros(user_row["id"])
    unlimited = budget < 0
    return {
        "usd": None if unlimited else round(budget / 1_000_000, 2),
        "unlimited": unlimited,
        # False while the user is still on the global default - the account
        # page says "we'll set this for you when you buy credits" instead of
        # presenting the number as a choice they made.
        "is_custom": user_row["monthly_budget_micros"] is not None,
        "spent_month_usd": round(spent / 1_000_000, 2),
        "remaining_usd": None if unlimited else round(
            max(0, budget - spent) / 1_000_000, 2
        ),
        "min_usd": round(MIN_MONTHLY_LIMIT_MICROS / 1_000_000, 2),
        "max_usd": round(MAX_MONTHLY_LIMIT_MICROS / 1_000_000, 2),
        "resets_at": _next_month_start_iso(),
    }


def usage_summary_month(email: str) -> Optional[dict]:
    """Per-user view of this month's spend and remaining limit, for admin
    display. Reports both the raw cost (what we pay Anthropic) and the
    credit-dollar figure the limit is measured in."""
    user = get_user_by_email(_normalize_email(email))
    if not user:
        return None
    raw = usage_month_micros(user["id"])
    spent = usage_month_credit_micros(user["id"])
    budget = _budget_from_row(user)
    return {
        "email": user["email"],
        "raw_micros": raw,
        "spent_micros": spent,
        "budget_micros": budget,
        "unlimited": budget < 0,
        "remaining_micros": None if budget < 0 else max(0, budget - spent),
    }


# ---------- billing: prepaid credits + subscription state ----------

def credit_balance_micros(user_id: int) -> int:
    """The user's prepaid balance: every purchase/grant minus every marked-up
    usage deduction. Can go slightly negative - output tokens aren't known
    until a request finishes, so the request that empties the balance is
    allowed to complete and the next one is refused (same shape as the monthly
    budget check)."""
    with _db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) AS total FROM credits_ledger "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["total"])


def add_credits(
    user_id: int,
    amount_micros: int,
    kind: str,
    stripe_ref: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    """Append one credit row. When stripe_ref is set, the UNIQUE constraint
    makes the insert idempotent - Stripe retries webhook deliveries and the
    /api/billing/refresh self-heal re-lists past checkouts, so the same
    purchase may be offered more than once. Returns True if a row was written
    (False = duplicate stripe_ref, already credited)."""
    with _db() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO credits_ledger "
            "(user_id, amount_micros, kind, stripe_ref, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount_micros, kind, stripe_ref, note, _now().isoformat()),
        )
        written = cur.rowcount > 0
    # First money in gets a monthly limit to match. Deliberately after the
    # insert so the balance it snapshots includes this credit, and skipped for
    # a duplicate so a webhook retry can't move a limit the user has since set.
    if written and amount_micros > 0 and kind in ("purchase", "grant"):
        ensure_monthly_limit_default(user_id)
    return written


def credit_headroom_micros(user_id: int) -> int:
    """How much more this user may buy before hitting MAX_CREDIT_BALANCE. A
    balance run negative by a final overshooting request doesn't earn extra
    headroom, hence the clamp at the cap."""
    return max(0, min(
        MAX_CREDIT_BALANCE_MICROS,
        MAX_CREDIT_BALANCE_MICROS - credit_balance_micros(user_id),
    ))


def pack_affordable(user_id: int, cents: int) -> bool:
    """True if buying this pack would leave the balance at or under the cap."""
    return cents * 10_000 <= credit_headroom_micros(user_id)


def grant_credits(email: str, usd: float, note: Optional[str] = None) -> bool:
    """Admin: grant (or claw back, with a negative amount) credits by email."""
    user = get_user_by_email(_normalize_email(email))
    if not user:
        return False
    return add_credits(
        user["id"], int(round(usd * 1_000_000)), "grant", note=note or "admin grant"
    )


def credit_history(user_id: int, limit: int = 10) -> list[dict]:
    """Most recent credit-ledger rows for the account page."""
    with _db() as db:
        rows = db.execute(
            "SELECT amount_micros, kind, note, created_at FROM credits_ledger "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "amount_usd": round(r["amount_micros"] / 1_000_000, 4),
            "kind": r["kind"],
            "note": r["note"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def set_stripe_customer(user_id: int, customer_id: str) -> None:
    """Record the Stripe Customer for a user, first writer wins (a concurrent
    checkout could race to create two customers; keeping the first stored id
    consistent matters more than which one wins)."""
    with _db() as db:
        db.execute(
            "UPDATE users SET stripe_customer_id = ? "
            "WHERE id = ? AND stripe_customer_id IS NULL",
            (customer_id, user_id),
        )


def get_user_by_stripe_customer(customer_id: str) -> Optional[sqlite3.Row]:
    if not customer_id:
        return None
    with _db() as db:
        return db.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()


def set_subscription(
    user_id: int, status: Optional[str], period_end_iso: Optional[str]
) -> None:
    """Mirror Stripe subscription state onto the user row (called from webhook
    handlers and the refresh self-heal). Never lets a Stripe event downgrade a
    'comp' user: comp is admin-granted, so e.g. cancelling an old paid
    subscription must not revoke complimentary access."""
    with _db() as db:
        row = db.execute(
            "SELECT subscription_status FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return
        if row["subscription_status"] == "comp" and status not in ("active", "trialing"):
            return
        db.execute(
            "UPDATE users SET subscription_status = ?, subscription_period_end = ? "
            "WHERE id = ?",
            (status, period_end_iso, user_id),
        )


def set_comp(email: str, comp: bool) -> bool:
    """Admin: grant or revoke complimentary subscription access. Granting also
    clears any cancellation (comp is an override, so it shouldn't leave the
    user locked out by an earlier cancel). Revoking clears the status entirely;
    if the user also has a real Stripe subscription, the next webhook or
    refresh restores it."""
    with _db() as db:
        if comp:
            cur = db.execute(
                "UPDATE users SET subscription_status = 'comp', "
                "subscription_period_end = NULL, canceled_at = NULL, "
                "access_ends_at = NULL WHERE email = ?",
                (_normalize_email(email),),
            )
        else:
            cur = db.execute(
                "UPDATE users SET subscription_status = NULL, "
                "subscription_period_end = NULL WHERE email = ?",
                (_normalize_email(email),),
            )
        return cur.rowcount > 0


def _parse_ts(iso: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO timestamp into an aware UTC datetime, or None if it
    is missing/unparseable. Naive values are assumed UTC (everything we write
    goes through _now(), which is aware, but hand-edited rows happen)."""
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def in_trial(user_row) -> bool:
    """True while the user is inside their signup trial - the window in which
    cancelling means never being charged for a subscription."""
    end = _parse_ts(user_row["trial_ends_at"])
    return end is not None and _now() <= end


def is_canceled(user_row) -> bool:
    """True once the user has asked to cancel, whether or not access has
    actually lapsed yet (post-trial cancellations keep access until the paid
    period runs out)."""
    return bool(user_row["canceled_at"])


def subscription_ok(user_row) -> bool:
    """True when this user's subscription entitles them to use the app now.

    Order matters: comp overrides everything; a cancellation then decides on
    its own recorded end time (exactly, with no grace - the user keeps what
    they already have and not a day more). Otherwise active/trialing hold
    until the period ends, with SUB_GRACE extended only to paid subscriptions
    so a late renewal webhook can't lock out a paying customer.
    """
    status = user_row["subscription_status"]
    if status == "comp":
        return True
    if is_canceled(user_row):
        end = _parse_ts(user_row["access_ends_at"])
        return end is not None and _now() <= end
    if status not in _SUB_OK_STATUSES:
        return False
    end = _parse_ts(user_row["subscription_period_end"])
    if end is None:
        return True
    if status == "trialing":
        return _now() <= end
    return _now() <= end + SUB_GRACE


def purchase_blocked(user_row) -> Optional[dict]:
    """Why this user may not buy more credits, or None if they may.

    The balance cap is checked first because it applies to everyone, comped
    accounts included - it's about how much money we're willing to take, not
    about entitlement. Past that, buying requires a subscription that is
    currently good AND not cancelled, so the default for an account with no
    live subscription is 'blocked', and someone winding down can't stock up on
    fuel they won't be able to burn.
    """
    if credit_balance_micros(user_row["id"]) >= MAX_CREDIT_BALANCE_MICROS:
        return {
            "code": "credit_limit_reached",
            "message": f"You already have the maximum ${MAX_CREDIT_BALANCE_USD:.0f} "
                       f"in credits. You can buy more once you've used some.",
        }
    if user_row["subscription_status"] == "comp":
        return None
    if is_canceled(user_row):
        return {
            "code": "subscription_canceled",
            "message": "Your subscription is cancelled — resume it to buy credits.",
        }
    if not subscription_ok(user_row):
        return {
            "code": "subscription_required",
            "message": "An active subscription is required to buy credits.",
        }
    return None


def can_purchase_credits(user_row) -> bool:
    return purchase_blocked(user_row) is None


def cancel_subscription(user_id: int) -> Optional[dict]:
    """Record a cancellation. Access always runs to the end of whatever the
    user already has - the rest of the trial, or the period they've paid for -
    because credits are non-refundable and cutting access early would strand a
    balance they can no longer spend. Returns a description of what happened,
    or None if the user doesn't exist / was already cancelled."""
    user = get_user_by_id(user_id)
    if user is None or is_canceled(user):
        return None
    now = _now()
    access_end = entitlement_end(user) or now
    if access_end < now:
        access_end = now
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = ?, access_ends_at = ? WHERE id = ?",
            (now.isoformat(), access_end.isoformat(), user_id),
        )
    return {
        "in_trial": in_trial(user),
        "access_ends_at": access_end.isoformat(),
    }


def clear_cancellation(user_id: int) -> None:
    """Unconditionally drop the cancel state. Used when a brand-new
    subscription starts, where resume_subscription's "hasn't lapsed yet" guard
    would otherwise leave a stale cancellation pinning access shut."""
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = NULL, access_ends_at = NULL WHERE id = ?",
            (user_id,),
        )


def end_access_now(user_id: int) -> None:
    """Cut access off immediately (Stripe deleted the subscription outright).
    An existing canceled_at is preserved so a cancellation the user already
    made keeps its original timestamp."""
    now = _now().isoformat()
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = COALESCE(canceled_at, ?), "
            "access_ends_at = ? WHERE id = ?",
            (now, now, user_id),
        )


def entitlement_end(user_row) -> Optional[datetime]:
    """The last moment this user is entitled to access, ignoring any
    cancellation: the later of the trial end and the paid-through date. This
    is what a cancellation freezes access_ends_at to, and what can_resume()
    measures against - a Stripe-side deletion can still pull access_ends_at
    forward (end_access_now), so the two aren't always the same value."""
    ends = [
        ts for ts in (
            _parse_ts(user_row["trial_ends_at"]),
            _parse_ts(user_row["subscription_period_end"]),
        ) if ts is not None
    ]
    return max(ends) if ends else None


def can_resume(user_row) -> bool:
    """True when a cancellation can still be undone, so the UI knows to offer
    Resume rather than a fresh Subscribe."""
    if not is_canceled(user_row):
        return False
    end = entitlement_end(user_row)
    return end is None or _now() <= end


def resume_subscription(user_id: int) -> bool:
    """Undo a cancellation, restoring access and the ability to buy credits.
    Allowed for as long as the user still has time left on the trial or paid
    period they cancelled - including right after an in-trial cancellation,
    which is exactly when someone is most likely to change their mind.
    Returns False when there is nothing left to resume; that needs a fresh
    subscription."""
    user = get_user_by_id(user_id)
    if user is None or not can_resume(user):
        return False
    clear_cancellation(user_id)
    return True


def billing_blocked(user_row) -> Optional[dict]:
    """Why this user may not spend API dollars right now, or None if they may.
    The failure codes drive distinct UI on the account page."""
    if not BILLING_REQUIRED:
        return None
    if not subscription_ok(user_row):
        if is_canceled(user_row):
            return {
                "code": "subscription_canceled",
                "message": "Your subscription is cancelled and access has ended. "
                           "Subscribe again to keep using the Arbiter.",
            }
        return {
            "code": "subscription_required",
            "message": "An active subscription is required to use the Arbiter.",
        }
    if credit_balance_micros(user_row["id"]) <= 0:
        return {
            "code": "credits_required",
            "message": "You're out of usage credits.",
        }
    return None


def require_billing(user_row) -> None:
    """Gate for the endpoints that spend API dollars (/api/chat,
    /api/deckbuilder). 402 with a structured detail the frontends turn into a
    'visit your Account page' notice. No-op until BILLING_REQUIRED is set."""
    blocked = billing_blocked(user_row)
    if blocked:
        raise HTTPException(status_code=402, detail=blocked)


# ---------- user / password ops ----------

def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _insert_user(email: str, pw_hash: str, name: Optional[str] = None) -> int:
    """Insert a pre-normalized email and pre-computed hash. Caller owns hashing
    so register() can hash unconditionally (constant timing) without this
    function hashing a second time.

    Every new account starts on a TRIAL_DAYS free trial. The trial only opens
    the subscription half of the gate - usage still needs purchased credits -
    so it is a "try it for a week, pay only for what you use" window rather
    than free API spend.

    Accounts are approved on creation: the money gate (subscription + credits)
    is what actually limits usage, so making people wait for a human to let
    them in bought nothing. `approved` survives as the revoke switch - see
    set_approved - so an abusive account can still be shut off and later let
    back in.

    No credits are granted here, and none should be. A new account's balance is
    $0.00 and stays there until money moves: a paid Checkout session
    (billing._credit_paid_session) or a deliberate admin grant (grant_credits)
    are the only two writers of positive credits_ledger rows. Registration is
    open, so a welcome bonus here would be an open invitation to farm accounts
    for free API spend.
    """
    now = _now()
    trial_end = (now + timedelta(days=TRIAL_DAYS)).isoformat()
    with _db() as db:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, name, approved, is_admin, "
            "subscription_status, subscription_period_end, trial_ends_at, created_at) "
            "VALUES (?, ?, ?, 1, 0, 'trialing', ?, ?, ?)",
            (email, pw_hash, name, trial_end, trial_end, now.isoformat()),
        )
        return cur.lastrowid


def create_user(email: str, password: str, name: Optional[str] = None) -> int:
    email = _normalize_email(email)
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    return _insert_user(email, _ph.hash(password), _clean_name(name))


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def update_name(user_id: int, name: Optional[str]) -> None:
    """Set or clear the optional display name. NULL means "unset", in which
    case the UI falls back to the email's local-part."""
    with _db() as db:
        db.execute(
            "UPDATE users SET name = ? WHERE id = ?", (_clean_name(name), user_id)
        )


def update_password(user_id: int, new_password: str) -> None:
    if len(new_password) < PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    pw_hash = _ph.hash(new_password)
    with _db() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
        # Invalidate every existing session on password change.
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------- sessions ----------

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _db() as db:
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now.isoformat(), (now + SESSION_TTL).isoformat()),
        )
    return token


def revoke_session(token: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def get_user_by_session(token: str) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute(
            "SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_hash_token(token), _now().isoformat()),
        ).fetchone()


# ---------- reset tokens ----------

def create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _db() as db:
        db.execute(
            "INSERT INTO reset_tokens (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now.isoformat(), (now + RESET_TOKEN_TTL).isoformat()),
        )
    return token


def consume_reset_token(token: str) -> Optional[int]:
    """Return user_id if the token is valid and unused, marking it used. Else None."""
    th = _hash_token(token)
    now_iso = _now().isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT user_id FROM reset_tokens "
            "WHERE token_hash = ? AND expires_at > ? AND used_at IS NULL",
            (th, now_iso),
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE reset_tokens SET used_at = ? WHERE token_hash = ?", (now_iso, th))
        return row["user_id"]


# ---------- admin ops (called by admin.py) ----------

def list_users() -> list[sqlite3.Row]:
    # SELECT * so callers can pass rows straight to subscription_ok() /
    # monthly_limit_view(), which read the trial, cancellation and limit columns.
    with _db() as db:
        return db.execute("SELECT * FROM users ORDER BY created_at").fetchall()


def count_admins() -> int:
    with _db() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]


def set_approved(email: str, approved: bool) -> bool:
    """Suspend (False) or reinstate (True) an account. Signups arrive approved,
    so this is the moderation switch rather than an intake queue - and it moves
    in both directions, so a revoked account can always be let back in."""
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET approved = ? WHERE email = ?",
            (1 if approved else 0, _normalize_email(email)),
        )
        return cur.rowcount > 0


def set_admin(email: str, is_admin: bool) -> bool:
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET is_admin = ? WHERE email = ?",
            (1 if is_admin else 0, _normalize_email(email)),
        )
        return cur.rowcount > 0


def delete_user(email: str) -> bool:
    with _db() as db:
        cur = db.execute("DELETE FROM users WHERE email = ?", (_normalize_email(email),))
        return cur.rowcount > 0


# ---------- FastAPI integration ----------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterReq(BaseModel):
    email: str
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
    # Optional display name. Bounded generously here; _clean_name trims and caps
    # it to MAX_NAME_LEN before storage.
    name: Optional[str] = Field(default=None, max_length=200)


class LoginReq(BaseModel):
    email: str
    # Same cap as registration, so no legitimately-set password is rejected
    # here while oversized bodies never reach Argon2.
    password: str = Field(max_length=PASSWORD_MAX_LEN)
    # When false, the session cookie is dropped when the browser closes; when
    # true (the default, matching the "keep me signed in" checkbox), it persists
    # for SESSION_TTL. Defaults true so an omitted field keeps prior behavior.
    remember: bool = True


class ResetReq(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


class NameReq(BaseModel):
    # Bounded generously here; _clean_name trims to MAX_NAME_LEN and turns a
    # blank value into NULL (display falls back to the email's local-part).
    name: Optional[str] = Field(default=None, max_length=200)


class PasswordLinkReq(BaseModel):
    # The caller's CURRENT password, re-entered to authorize a reset link.
    password: str = Field(max_length=PASSWORD_MAX_LEN)


def _client_ip(request: Request) -> str:
    """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

    The leftmost XFF entries are written by the client and cannot be trusted.
    Each proxy *appends* the address it received the connection from, so with
    TRUSTED_PROXY_HOPS proxies in front of us the real client is that many
    entries from the right. If the chain is shorter than configured (anomaly
    or misconfig), fall back to the direct peer, which cannot be spoofed.
    """
    direct = request.client.host if request.client else "0.0.0.0"
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    idx = len(parts) - TRUSTED_PROXY_HOPS
    if 0 <= idx < len(parts):
        return parts[idx]
    return direct


def _is_secure(request: Request) -> bool:
    # Render and Fly terminate TLS at the proxy. Trust X-Forwarded-Proto when
    # present; uvicorn with --proxy-headers will also rewrite request.url.scheme.
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def base_url(request: Request) -> str:
    """Absolute origin for links and redirects we hand to users (reset links,
    Stripe checkout returns). APP_BASE_URL wins when set; otherwise it is
    derived from the incoming request so links are correct with no config."""
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if base:
        return base
    scheme = "https" if _is_secure(request) else request.url.scheme
    return f"{scheme}://{request.headers.get('host', '')}"


def _set_session_cookie(
    response: Response, token: str, request: Request, remember: bool = True
) -> None:
    # remember -> persistent cookie for SESSION_TTL; otherwise a session cookie
    # (no Max-Age) the browser drops on close. The server-side session row keeps
    # its own SESSION_TTL expiry either way; a session cookie just means the
    # browser stops presenting the token sooner.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()) if remember else None,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
    )


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return get_user_by_session(token)


def require_user(request: Request) -> sqlite3.Row:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user["approved"]:
        raise HTTPException(status_code=403, detail="Account suspended")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    """Gate for the admin panel and its API. Authenticated + not suspended +
    admin."""
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_same_origin(request: Request) -> None:
    """Defense-in-depth for high-privilege state-changing admin calls. SameSite
    =Lax already blocks cross-site cookie use; this additionally rejects any
    request whose Origin (when the browser sends one) isn't our own host. A
    missing Origin (common on same-origin GETs) is allowed and left to SameSite."""
    origin = request.headers.get("origin")
    if not origin:
        return
    from urllib.parse import urlparse
    origin_host = urlparse(origin).netloc.lower()
    host = (request.headers.get("host") or "").lower()
    if origin_host and host and origin_host != host:
        raise HTTPException(status_code=403, detail="Cross-origin request refused.")


@router.post("/register", status_code=201)
def register(req: RegisterReq, request: Request):
    ip = _client_ip(request)
    if _register_limit.blocked(ip):
        raise HTTPException(429, "Too many registration attempts. Please wait and try again.")

    try:
        email = _normalize_email(req.email)
    except EmailNotValidError:
        _register_limit.record(ip)
        raise HTTPException(400, "Invalid email address.")
    if len(req.password) < PASSWORD_MIN_LEN:
        raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LEN} characters.")

    _register_limit.record(ip)

    # Always compute one Argon2 hash so response time doesn't reveal whether
    # the email already exists (the hash dominates timing; the existence check
    # and the insert are negligible by comparison). Same response either way.
    pw_hash = _ph.hash(req.password)
    if not get_user_by_email(email):
        try:
            _insert_user(email, pw_hash, _clean_name(req.name))
        except sqlite3.IntegrityError:
            pass  # Lost a race; treat as success.

    return {
        "message": "Account created. You can sign in now — your free trial "
                   f"runs for {TRIAL_DAYS} days."
    }


@router.post("/login")
def login(req: LoginReq, request: Request, response: Response):
    try:
        email = _normalize_email(req.email)
    except EmailNotValidError:
        raise HTTPException(401, "Invalid Credentials. Check your input.")

    ip = _client_ip(request)
    fail_key = f"{ip}\x00{email}"
    if _login_fail.blocked(fail_key) or _login_ip.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Please wait and try again.")

    user = get_user_by_email(email)
    # Verify against a real hash even when the user doesn't exist, so the
    # timing of failed logins doesn't leak registration status.
    pw_ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, req.password)

    if not user or not pw_ok:
        _login_fail.record(fail_key)
        _login_ip.record(ip)
        raise HTTPException(401, "Invalid Credentials. Check your input.")

    if not user["approved"]:
        # Reached only for an account an admin has revoked - registration
        # approves by default. The credentials were correct, so there is
        # nothing to hide by being vague here.
        raise HTTPException(403, "This account has been suspended.")

    # Clear the per-account counter on success. The per-IP counter is left to
    # age out so one success can't reset a spraying attack from the same IP.
    _login_fail.clear(fail_key)
    token = create_session(user["id"])
    _set_session_cookie(response, token, request, remember=req.remember)
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(token)
    _clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/me")
def me(user: sqlite3.Row = Depends(require_user)):
    # Spend is reported in credit dollars, the same units as the monthly limit
    # and the balance, so the header's "$0.40 / $5.00 this month" reads against
    # the number the user actually watches drop rather than raw Anthropic cost.
    spent = usage_month_credit_micros(user["id"])
    budget = _budget_from_row(user)
    unlimited = budget < 0
    return {
        "email": user["email"],
        "name": user["name"],
        "is_admin": bool(user["is_admin"]),
        "spent_micros": spent,
        "budget_micros": budget,
        "unlimited": unlimited,
        "spent_usd": round(spent / 1_000_000, 4),
        "budget_usd": None if unlimited else round(budget / 1_000_000, 2),
        # Billing state for the header + account page. Additive - existing
        # consumers of the fields above are unaffected.
        "billing_required": BILLING_REQUIRED,
        "subscription_status": user["subscription_status"],
        "subscription_ok": subscription_ok(user),
        "in_trial": in_trial(user),
        "canceled": is_canceled(user),
        "credit_balance_usd": round(credit_balance_micros(user["id"]) / 1_000_000, 2),
    }


@router.post("/name")
def set_display_name(
    req: NameReq, request: Request, user: sqlite3.Row = Depends(require_user)
):
    """Set or clear the sidebar display name. The email is deliberately NOT
    editable: it is the account's identity for sign-in, reset links, and the
    Stripe customer, so changing it here would silently split those."""
    require_same_origin(request)
    update_name(user["id"], req.name)
    return {"ok": True, "name": _clean_name(req.name)}


@router.post("/password-reset-link")
def self_service_reset_link(
    req: PasswordLinkReq, request: Request, user: sqlite3.Row = Depends(require_user)
):
    """Issue a single-use reset link for the caller's own account - the
    self-service equivalent of `python admin.py reset <email>`, reusing the
    same token table and /reset page.

    The current password is required: a session cookie alone must not be able
    to change the password, or an unattended browser (or a stolen cookie)
    could lock the real owner out. Spending the link invalidates every
    session, so the user signs in again with the new password.
    """
    require_same_origin(request)
    if _pwlink_limit.hit(str(user["id"])):
        raise HTTPException(429, "Too many attempts. Please wait and try again.")
    if not verify_password(user["password_hash"], req.password):
        raise HTTPException(403, "That password is incorrect.")
    token = create_reset_token(user["id"])
    return {
        "reset_url": f"{base_url(request)}/reset?token={quote(token)}",
        "expires_hours": int(RESET_TOKEN_TTL.total_seconds() // 3600),
    }


@router.post("/reset")
def reset_password(req: ResetReq, request: Request, response: Response):
    user_id = consume_reset_token(req.token)
    if user_id is None:
        raise HTTPException(400, "Invalid or expired reset token.")
    try:
        update_password(user_id, req.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _clear_session_cookie(response, request)
    return {"ok": True, "message": "Password updated. Please sign in again."}


# ---------- conversation history ----------
# Each user keeps only their most recent chats in the sidebar; older ones are
# pruned so the table can't grow without bound on the persistent disk.
MAX_CONVERSATIONS = 5


def save_conversation(
    user_id: int,
    conv_id: int | None,
    title: str,
    fmt: str,
    messages_json: str,
) -> int:
    """Insert a new conversation or update an existing one owned by user_id,
    then prune everything past the MAX_CONVERSATIONS most recent. Returns the
    conversation id (new or existing)."""
    now = _now().isoformat()
    with _db() as db:
        row = None
        if conv_id is not None:
            row = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()

        if row:
            db.execute(
                "UPDATE conversations SET title = ?, format = ?, messages = ?, "
                "updated_at = ? WHERE id = ?",
                (title, fmt, messages_json, now, row["id"]),
            )
            cid = row["id"]
        else:
            cur = db.execute(
                "INSERT INTO conversations (user_id, title, format, messages, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, title, fmt, messages_json, now, now),
            )
            cid = cur.lastrowid

        # Prune older conversations beyond the cap for this user.
        db.execute(
            """
            DELETE FROM conversations
             WHERE user_id = ?
               AND id NOT IN (
                 SELECT id FROM conversations
                  WHERE user_id = ?
                  ORDER BY updated_at DESC, id DESC
                  LIMIT ?
               )
            """,
            (user_id, user_id, MAX_CONVERSATIONS),
        )
        return cid


def list_conversations(user_id: int, limit: int = MAX_CONVERSATIONS) -> list[dict]:
    """The user's most recent conversations (metadata only, newest first)."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, title, format, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "format": r["format"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_conversation(user_id: int, conv_id: int) -> dict | None:
    """Full conversation (including its stored messages) if owned by user_id."""
    with _db() as db:
        r = db.execute(
            "SELECT id, title, format, messages, updated_at FROM conversations "
            "WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "title": r["title"],
        "format": r["format"],
        "messages": r["messages"],
        "updated_at": r["updated_at"],
    }


def delete_conversation(user_id: int, conv_id: int) -> bool:
    with _db() as db:
        cur = db.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        return cur.rowcount > 0
