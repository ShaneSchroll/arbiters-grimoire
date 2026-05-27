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

LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILS = 10

PASSWORD_MIN_LEN = 12

_ph = PasswordHasher()
# Used to keep the argon2 verify cost constant when the email doesn't exist,
# so an attacker can't tell registered emails from unregistered ones by timing.
_DUMMY_HASH = _ph.hash("not-a-real-password-only-for-timing-safety")

# (ip, email) -> list of recent failure timestamps (unix seconds).
_login_fails: dict[tuple[str, str], list[float]] = {}


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
            """
        )


# ---------- user / password ops ----------

def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(email: str, password: str) -> int:
    email = _normalize_email(email)
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    pw_hash = _ph.hash(password)
    with _db() as db:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, approved, is_admin, created_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (email, pw_hash, _now().isoformat()),
        )
        return cur.lastrowid


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
            "SELECT id, email, approved, is_admin, created_at FROM users ORDER BY created_at"
        ).fetchall()


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


# ---------- throttling ----------

def _check_throttle(ip: str, email: str) -> bool:
    """Return True if this (ip, email) is currently blocked."""
    key = (ip, email)
    cutoff = time.time() - LOGIN_WINDOW.total_seconds()
    fails = [t for t in _login_fails.get(key, []) if t > cutoff]
    _login_fails[key] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def _record_failure(ip: str, email: str) -> None:
    _login_fails.setdefault((ip, email), []).append(time.time())


def _clear_failures(ip: str, email: str) -> None:
    _login_fails.pop((ip, email), None)


# ---------- FastAPI integration ----------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterReq(BaseModel):
    email: str
    password: str = Field(min_length=PASSWORD_MIN_LEN)


class LoginReq(BaseModel):
    email: str
    password: str


class ResetReq(BaseModel):
    token: str
    new_password: str = Field(min_length=PASSWORD_MIN_LEN)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _is_secure(request: Request) -> bool:
    # Render and Fly terminate TLS at the proxy. Trust X-Forwarded-Proto when
    # present; uvicorn with --proxy-headers will also rewrite request.url.scheme.
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
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


@router.post("/register", status_code=201)
def register(req: RegisterReq):
    try:
        email = _normalize_email(req.email)
    except EmailNotValidError:
        raise HTTPException(400, "Invalid email address.")
    if len(req.password) < PASSWORD_MIN_LEN:
        raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LEN} characters.")

    # Same response whether the email is new or already registered, to avoid
    # disclosing which addresses have accounts.
    if not get_user_by_email(email):
        try:
            create_user(email, req.password)
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
        raise HTTPException(401, "Invalid credentials.")

    ip = _client_ip(request)
    if _check_throttle(ip, email):
        raise HTTPException(429, "Too many failed attempts. Please wait and try again.")

    user = get_user_by_email(email)
    # Verify against a real hash even when the user doesn't exist, so the
    # timing of failed logins doesn't leak registration status.
    pw_ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, req.password)

    if not user or not pw_ok:
        _record_failure(ip, email)
        raise HTTPException(401, "Invalid credentials.")

    if not user["approved"]:
        # Don't differentiate "wrong password" from "not approved" to outsiders;
        # the approval message comes through the registration response.
        raise HTTPException(403, "Account not yet approved.")

    _clear_failures(ip, email)
    token = create_session(user["id"])
    _set_session_cookie(response, token, request)
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
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


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
    return {"ok": True, "message": "Password updated. Please sign in."}
