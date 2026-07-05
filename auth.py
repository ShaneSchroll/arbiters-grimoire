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
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("AUTH_DB_PATH", BASE_DIR / "users.db"))

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

# Trusted reverse proxies in front of the app, counted from the connection
# inward: Render alone = 1; Cloudflare -> Render = 2. The env var MUST match
# the real chain: too low and per-IP rate limits key on a proxy's (shared) IP;
# too high and clients can spoof their IP via X-Forwarded-For.
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))

# ---------- spend accounting ----------
DEFAULT_DAILY_BUDGET_MICROS = int(
    float(os.getenv("DAILY_BUDGET_USD", "1.00")) * 1_000_000
)

PRICING = {
    "claude-opus-4-8": {
        "input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50,
    },
}
# Fallback for an unrecognized model: the most expensive rate in each column,
# so an unknown model can never be under-billed past the budget.
_FALLBACK_RATE = {
    field: max(p[field] for p in PRICING.values())
    for field in ("input", "output", "cache_write", "cache_read")
}

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

# Cap on distinct keys any single limiter will track, so a flood of distinct
# keys (e.g. login failures with rotating emails) can't exhaust memory.
_MAX_TRACKED_KEYS = 20_000


class _RateLimiter:
    """In-memory sliding-window limiter keyed by an arbitrary string.

    NOT durable: state is per-process and resets on restart, and is not shared
    across workers. With a single worker (recommended for this deployment) it
    is sufficient; if you scale out, move these counters to the DB or a shared
    store, or each worker will enforce the limit independently.
    """

    def __init__(self, max_events: int, window: timedelta) -> None:
        self._max = max_events
        self._window = window.total_seconds()
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window
        kept = [t for t in self._hits.get(key, ()) if t > cutoff]
        if kept:
            self._hits[key] = kept
        else:
            self._hits.pop(key, None)
        return kept

    def _sweep(self, now: float) -> None:
        if len(self._hits) <= _MAX_TRACKED_KEYS:
            return
        cutoff = now - self._window
        self._hits = {
            k: recent
            for k, ts in self._hits.items()
            if (recent := [t for t in ts if t > cutoff])
        }

    def blocked(self, key: str) -> bool:
        """True if the key is already at/over the limit (no event recorded)."""
        with self._lock:
            return len(self._recent(key, time.time())) >= self._max

    def record(self, key: str) -> None:
        """Record one event against the key."""
        with self._lock:
            now = time.time()
            self._sweep(now)
            self._recent(key, now)
            self._hits.setdefault(key, []).append(now)

    def hit(self, key: str) -> bool:
        """Record one event and return True if the key is now over the limit."""
        with self._lock:
            now = time.time()
            self._sweep(now)
            events = self._recent(key, now)
            events.append(now)
            self._hits[key] = events
            return len(events) > self._max

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_login_fail = _RateLimiter(LOGIN_MAX_FAILS, LOGIN_WINDOW)
_login_ip = _RateLimiter(LOGIN_IP_MAX_FAILS, LOGIN_WINDOW)
_register_limit = _RateLimiter(REGISTER_MAX, REGISTER_WINDOW)
_chat_limit = _RateLimiter(CHAT_MAX, CHAT_WINDOW)
_card_limit = _RateLimiter(CARD_MAX, CARD_WINDOW)


def chat_rate_limited(user_id: int) -> bool:
    """Record a chat request for this user; True if they are now over CHAT_MAX."""
    return _chat_limit.hit(str(user_id))


def card_rate_limited(user_id: int) -> bool:
    """Record a card lookup for this user; True if they are now over CARD_MAX."""
    return _card_limit.hit(str(user_id))


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


def init_db() -> None:
    with _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              email         TEXT    NOT NULL UNIQUE,
              password_hash TEXT    NOT NULL,
              approved      INTEGER NOT NULL DEFAULT 0,
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
    if "daily_budget_micros" not in cols:
        # Nullable: NULL means "use DEFAULT_DAILY_BUDGET_MICROS". A negative
        # value means unlimited. A non-negative value is a per-user override.
        db.execute("ALTER TABLE users ADD COLUMN daily_budget_micros INTEGER")
    if "opus_allowed" not in cols:
        # Vestigial: per-user Opus gating was removed (every user can use every
        # model now). Retained so existing and fresh DBs share one schema;
        # nothing reads it anymore.
        db.execute("ALTER TABLE users ADD COLUMN opus_allowed INTEGER NOT NULL DEFAULT 0")


def _cleanup_expired() -> None:
    """Delete rows that can no longer authenticate anything, so the sessions
    and reset_tokens tables don't grow without bound on the persistent disk."""
    now_iso = _now().isoformat()
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        db.execute(
            "DELETE FROM reset_tokens WHERE expires_at <= ? OR used_at IS NOT NULL",
            (now_iso,),
        )


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
    with _db() as db:
        db.execute(
            "INSERT INTO usage_ledger (user_id, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_micros, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, model, input_tokens, output_tokens,
                cache_write_tokens, cache_read_tokens, cost, _now().isoformat(),
            ),
        )
    return cost


def _utc_day_start_iso() -> str:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def usage_today_micros(user_id: int) -> int:
    """Total spend (micro-dollars) for this user since 00:00 UTC today."""
    with _db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(cost_micros), 0) AS total FROM usage_ledger "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, _utc_day_start_iso()),
        ).fetchone()
    return int(row["total"])


def _budget_for(user_id: int) -> int:
    """This user's daily budget in micro-dollars. NULL override -> the default;
    a negative override -> unlimited (returned as a sentinel the caller treats
    as no cap)."""
    with _db() as db:
        row = db.execute(
            "SELECT daily_budget_micros FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return DEFAULT_DAILY_BUDGET_MICROS
    override = row["daily_budget_micros"]
    if override is None:
        return DEFAULT_DAILY_BUDGET_MICROS
    return int(override)


def daily_budget_exceeded(user_id: int) -> bool:
    """True if the user has already met or passed their daily budget. Checked
    BEFORE a request starts; since token cost isn't known until generation
    finishes, the request that crosses the line is allowed to complete and the
    next one is refused. Per-request overshoot is bounded by max_tokens and the
    tool-loop cap in server.py."""
    budget = _budget_for(user_id)
    if budget < 0:
        return False  # unlimited
    return usage_today_micros(user_id) >= budget


def set_daily_budget(email: str, micros: Optional[int]) -> bool:
    """Set a per-user daily budget override. None clears it (back to default);
    a negative value means unlimited. Called by admin.py."""
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET daily_budget_micros = ? WHERE email = ?",
            (micros, _normalize_email(email)),
        )
        return cur.rowcount > 0


def usage_summary_today(email: str) -> Optional[dict]:
    """Per-user view of today's spend and remaining budget, for admin display."""
    user = get_user_by_email(_normalize_email(email))
    if not user:
        return None
    spent = usage_today_micros(user["id"])
    budget = _budget_for(user["id"])
    return {
        "email": user["email"],
        "spent_micros": spent,
        "budget_micros": budget,
        "unlimited": budget < 0,
        "remaining_micros": None if budget < 0 else max(0, budget - spent),
    }


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
    function hashing a second time."""
    with _db() as db:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, name, approved, is_admin, created_at) "
            "VALUES (?, ?, ?, 0, 0, ?)",
            (email, pw_hash, name, _now().isoformat()),
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
    with _db() as db:
        return db.execute(
            "SELECT id, email, approved, is_admin, "
            "daily_budget_micros, created_at FROM users ORDER BY created_at"
        ).fetchall()


def count_admins() -> int:
    with _db() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]


def set_approved(email: str, approved: bool) -> bool:
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
        raise HTTPException(status_code=403, detail="Account not approved")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    """Gate for the admin panel and its API. Authenticated + approved + admin."""
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
        "message": "Registration submitted. An administrator must approve "
                   "the account before you can sign in."
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
        # Don't differentiate "wrong password" from "not approved" to outsiders;
        # the approval message comes through the registration response.
        raise HTTPException(403, "Account is pending approval.")

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
    spent = usage_today_micros(user["id"])
    budget = _budget_for(user["id"])
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
