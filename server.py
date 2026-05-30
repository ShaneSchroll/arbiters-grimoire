"""
server.py - Web backend for the MTG Rules Oracle.

Flow for each user question:
  1. Retrieve the most relevant rulebook chunks (BM25 over your PDF).
  2. Send them to Claude alongside an MTG-expert system prompt.
  3. If Claude asks to look up a card, call Scryfall and feed the result back.
  4. Return Claude's final answer plus the rule sources used.

Auth:
  The chat endpoint requires an approved, signed-in user. Account creation and
  approval are handled in auth.py + admin.py. See admin.py for bootstrap.

Run:  uvicorn server:app --port 8000
Behind a proxy (Render):
      uvicorn server:app --host 0.0.0.0 --port 8000 \\
              --proxy-headers --forwarded-allow-ips '*'
  so that request.url.scheme reports https and cookies get the Secure flag.
"""

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import MutableHeaders

import auth
from mtg_api import CARD_TOOL, lookup_card
from retriever import Retriever

# Matches rule citations Claude is asked to produce, e.g. "509.2" or "509.2a".
RULE_CITE = re.compile(r"\b(\d{3}\.\d+[a-z]?)\b")
MAX_SOURCES = 6

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

client = Anthropic()  # reads ANTHROPIC_API_KEY
retriever = Retriever()


def find_index_html() -> Path:
    """Locate index.html"""
    for candidate in (
        BASE_DIR / "static" / "index.html",
        BASE_DIR / "index.html",
        BASE_DIR / "index",
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Could not find index.html. Put it next to server.py "
        "(or in a 'static' subfolder)."
    )


INDEX_HTML = find_index_html()
PAGES_DIR = BASE_DIR / "pages"
TIPS_HTML = PAGES_DIR / "tips.html"
RULES_HTML = PAGES_DIR / "rules.html"
DOCS_JSON = BASE_DIR / "docs.json"
AUTH_PAGES = BASE_DIR / "auth_pages"


def _find_admin_html() -> Path | None:
    """Resolve admin.html across the flat-repo and built layouts. Returns None
    if not found, so the route can give a clear 'not deployed' message instead
    of crashing the whole app at import time."""
    for candidate in (
        AUTH_PAGES / "admin.html",
        BASE_DIR / "static" / "admin.html",
        PAGES_DIR / "admin.html",
        BASE_DIR / "admin.html",
    ):
        if candidate.exists():
            return candidate
    return None


ADMIN_HTML = _find_admin_html()

# Models available in the dropdown. Sonnet is the default - faster and cheaper
# for everyday questions; Opus is opt-in via the dropdown for hard questions.
ALLOWED_MODELS = {"claude-sonnet-4-6", "claude-opus-4-7"}
DEFAULT_MODEL = "claude-sonnet-4-6"

# Static, cacheable persona. Kept separate from the per-question rules text so
# it can be marked with cache_control and reused cheaply across requests.
SYSTEM_PERSONA = """You are the MTG Rules Oracle, a meticulous judge-level \
expert on Magic: The Gathering rules and card interactions.

How to answer:
- Reason strictly from the RULEBOOK EXCERPTS provided in the user turn. They \
are authoritative. If they are insufficient, say so plainly rather than \
guessing.
- Cite the specific rule numbers you rely on, e.g. "(509.2)".
- When a question names a specific card, use the lookup_card tool to get its \
exact current Oracle text before ruling - printed wording is often outdated.
- Walk through interactions step by step (priority, the stack, triggered \
abilities, state-based actions) so the player learns the "why".
- Be precise and concise. Distinguish what the rules state from your own \
inference, and flag genuinely ambiguous cases."""


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.init_db()
    yield


# Content-Security-Policy is tailored to what the app actually loads: same-origin
# assets, Google Fonts (stylesheet from googleapis, font files from gstatic), and
# data: SVGs used in CSS. 'unsafe-inline' in script-src is required only by the
# inline <script> blocks in the auth pages (login/register/reset); move those to
# external files to drop it and get real script-injection protection.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": _CSP,
}


class SecurityHeadersMiddleware:
    """Inject security headers at response-start. Pure ASGI (not
    BaseHTTPMiddleware) so it never buffers the body - the SSE chat stream
    keeps flushing chunks as they arrive."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(title="MTG Rules Oracle", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
# Gzip JSON / HTML over the wire. Big win for docs.json (~1MB → ~360KB),
# negligible cost. minimum_size skips tiny payloads that wouldn't benefit.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# Caps on chat input. Bound request cost so an approved (or compromised)
# account can't drive unbounded Anthropic spend with huge payloads.
MAX_MESSAGES = 15
MAX_MESSAGE_CHARS = 8_000
MAX_TOTAL_CHARS = 24_000


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    # No min_length: the client replays prior assistant turns verbatim and an
    # empty turn must not 422 the whole request. max_length is the cost guard.
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    model: str = DEFAULT_MODEL

    @field_validator("messages")
    @classmethod
    def _within_total_budget(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if sum(len(m.content) for m in v) > MAX_TOTAL_CHARS:
            raise ValueError(f"Conversation exceeds {MAX_TOTAL_CHARS} characters.")
        return v


def build_system(question: str):
    """Assemble the system prompt: cached persona + fresh retrieved rules."""
    hits = retriever.search(question, k=6)
    if hits:
        excerpts = "\n\n---\n\n".join(
            f"[{h['rule'] or h['id']}]\n{h['text']}" for h in hits
        )
    else:
        excerpts = "(No matching rulebook excerpts were found for this query.)"

    system = [
        {
            "type": "text",
            "text": SYSTEM_PERSONA,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"RULEBOOK EXCERPTS for the current question:\n\n{excerpts}",
        },
    ]
    sources = [
        {"rule": h["rule"] or h["id"], "text": h["text"]} for h in hits
    ]
    return system, sources


def filter_sources(answer: str, sources: list) -> list:
    """Keep only sources whose rule number was actually cited in the answer.

    Glossary chunks (rule is None, id like "chunk-0001") never match a
    citation directly, so they drop out — but Claude almost always cites the
    numbered rule the glossary entry points to, which IS in the retrieved set.
    Falls back to the top 2 by relevance if nothing was cited (rare).
    """
    cited = set(RULE_CITE.findall(answer))
    used = [s for s in sources if s["rule"] in cited]
    if not used:
        used = sources[:2]
    return used[:MAX_SOURCES]


def _sse(payload: dict) -> str:
    """Encode one Server-Sent Event with a JSON data field."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(req: ChatRequest, user=Depends(auth.require_user)):
    if auth.chat_rate_limited(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please wait a moment and try again.",
        )
    if auth.daily_budget_exceeded(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="Daily usage budget reached. It resets at 00:00 UTC.",
        )

    # Opus is admins-only by default; other users need an explicit grant. The
    # frontend hides the option for ineligible users, but never trust the
    # client - enforce here. An ineligible request silently runs on the
    # default (Sonnet) rather than erroring.
    permitted = ALLOWED_MODELS if auth.can_use_opus(user) else {DEFAULT_MODEL}
    model = req.model if req.model in permitted else DEFAULT_MODEL

    last_user = next(
        (m.content for m in reversed(req.messages) if m.role == "user"),
        "",
    )
    system, sources = build_system(last_user)
    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    def event_stream():
        """Server-Sent Events: delta chunks during generation, then a final
        'done' event carrying the filtered sources. The tool loop continues
        between streamed rounds - text from "let me look that up" rounds is
        streamed too, so the user sees what's happening live."""
        answer_parts: list[str] = []
        # Token usage summed across every API round-trip in this request. Each
        # round (including tool-use rounds) is a separate billable call, so we
        # add them up. Recorded in the finally below so a client disconnect or a
        # mid-stream error still bills for whatever was generated.
        usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

        def add_usage(u):
            if u is None:
                return
            usage["input"] += getattr(u, "input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0

        try:
            for _ in range(6):  # safety cap on tool round-trips
                with client.messages.stream(
                    model=model,
                    max_tokens=2048,
                    system=system,
                    tools=[CARD_TOOL],
                    messages=messages,
                ) as stream:
                    for chunk in stream.text_stream:
                        answer_parts.append(chunk)
                        yield _sse({"type": "delta", "text": chunk})
                    final = stream.get_final_message()
                add_usage(getattr(final, "usage", None))

                if final.stop_reason != "tool_use":
                    full = "".join(answer_parts)
                    yield _sse({
                        "type": "done",
                        "sources": filter_sources(full, sources),
                        "model": model,
                    })
                    return

                # Tool round-trip: run every card lookup, then loop back.
                messages.append({"role": "assistant", "content": final.content})
                tool_results = []
                for block in final.content:
                    if block.type == "tool_use" and block.name == "lookup_card":
                        result = lookup_card(block.input.get("name", ""))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })
                messages.append({"role": "user", "content": tool_results})

            yield _sse({
                "type": "error",
                "message": "The assistant exceeded the tool-use limit. Please rephrase.",
            })
        except Exception:
            yield _sse({"type": "error", "message": "Server error while generating."})
        finally:
            # Record metered usage no matter how the stream ended. Never let an
            # accounting failure surface to the client or mask the real outcome.
            if any(usage.values()):
                try:
                    auth.record_usage(
                        user["id"], model,
                        usage["input"], usage["output"],
                        usage["cache_write"], usage["cache_read"],
                    )
                except Exception:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Disable proxy buffering so chunks reach the browser immediately.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(INDEX_HTML)


@app.get("/pages/tips")
def tips_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(TIPS_HTML)


@app.get("/pages/rules")
def rules_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(RULES_HTML)


@app.get("/docs.json")
def docs_json(_user=Depends(auth.require_user)):
    """Powers the in-app rules docs page. Auth-gated like /api/chat -
    don't ship the cleaned rulebook to anonymous visitors. GZipMiddleware
    above compresses the ~1MB JSON down to ~360KB on the wire."""
    if not DOCS_JSON.exists():
        raise HTTPException(
            status_code=503,
            detail="docs.json is missing. Run `python ingest.py <pdf>` to generate it.",
        )
    return FileResponse(DOCS_JSON, media_type="application/json")


@app.get("/login")
def login_page():
    return FileResponse(AUTH_PAGES / "login.html")


@app.get("/register")
def register_page():
    return FileResponse(AUTH_PAGES / "register.html")


@app.get("/reset")
def reset_page():
    return FileResponse(AUTH_PAGES / "reset.html")


# ===================== Admin panel (web) =====================
#
# Mirrors every operation in admin.py so day-to-day management can happen in
# the browser, while the CLI remains fully functional as a fallback if the
# frontend ever has issues. Every endpoint requires an admin session; every
# state-changing endpoint also enforces same-origin as defense-in-depth.

from fastapi import APIRouter, Body  # noqa: E402  (kept local to this section)
from urllib.parse import quote  # noqa: E402

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_guard(request: Request):
    auth.require_same_origin(request)
    return auth.require_admin(request)


def _user_view(row) -> dict:
    """Shape a user row for the admin table, including today's spend."""
    budget = auth._budget_for(row["id"])
    spent = auth.usage_today_micros(row["id"])
    raw = row["daily_budget_micros"] if "daily_budget_micros" in row.keys() else None
    return {
        "email": row["email"],
        "approved": bool(row["approved"]),
        "is_admin": bool(row["is_admin"]),
        "opus_allowed": bool(row["opus_allowed"]) if "opus_allowed" in row.keys() else False,
        "created_at": row["created_at"],
        "budget_is_default": raw is None,
        "budget_unlimited": budget < 0,
        "budget_usd": None if budget < 0 else round(budget / 1_000_000, 2),
        "spent_usd": round(spent / 1_000_000, 4),
    }


@admin_router.get("/users")
def admin_list_users(_admin=Depends(auth.require_admin)):
    return {
        "users": [_user_view(u) for u in auth.list_users()],
        "default_budget_usd": round(auth.DEFAULT_DAILY_BUDGET_MICROS / 1_000_000, 2),
    }


def _require_existing(email: str):
    user = auth.get_user_by_email(auth._normalize_email(email))
    if not user:
        raise HTTPException(404, f"No such user: {email}")
    return user


@admin_router.post("/approve")
def admin_approve(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    approved = bool(payload.get("approved", True))
    if not auth.set_approved(email, approved):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/admin")
def admin_set_admin(payload: dict = Body(...), admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    make_admin = bool(payload.get("is_admin", False))
    target = _require_existing(email)
    # Guard against locking everyone out: never remove the last admin.
    if not make_admin and target["is_admin"] and auth.count_admins() <= 1:
        raise HTTPException(400, "Refusing to remove the only remaining admin.")
    if not auth.set_admin(email, make_admin):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/opus")
def admin_set_opus(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    allowed = bool(payload.get("opus_allowed", False))
    if not auth.set_opus_allowed(email, allowed):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/budget")
def admin_set_budget(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    mode = payload.get("mode")  # "usd" | "unlimited" | "default"
    if mode == "unlimited":
        micros = -1
    elif mode == "default":
        micros = None
    elif mode == "usd":
        try:
            usd = float(payload.get("usd"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid dollar amount.")
        if usd < 0:
            raise HTTPException(400, "Use 'unlimited' for no cap; usd must be >= 0.")
        micros = int(round(usd * 1_000_000))
    else:
        raise HTTPException(400, "mode must be one of: usd, unlimited, default.")
    if not auth.set_daily_budget(email, micros):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/delete")
def admin_delete(payload: dict = Body(...), admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    target = _require_existing(email)
    if target["is_admin"] and auth.count_admins() <= 1:
        raise HTTPException(400, "Refusing to delete the only remaining admin.")
    if target["email"] == admin["email"]:
        raise HTTPException(400, "You cannot delete your own account from the panel.")
    if not auth.delete_user(email):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/reset-link")
def admin_reset_link(payload: dict = Body(...), _admin=Depends(_admin_guard), request: Request = None):
    """Issue a single-use password reset link the admin can hand to the user.
    The token is never stored in plaintext (only its hash, see auth.py)."""
    email = payload.get("email", "")
    user = _require_existing(email)
    token = auth.create_reset_token(user["id"])
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not base and request is not None:
        # Derive from the incoming request so the link is correct without config.
        scheme = "https" if auth._is_secure(request) else request.url.scheme
        base = f"{scheme}://{request.headers.get('host', '')}"
    return {"ok": True, "reset_url": f"{base}/reset?token={quote(token)}"}


@admin_router.post("/create")
def admin_create(payload: dict = Body(...), _admin=Depends(_admin_guard), request: Request = None):
    """Create an account from the panel. No password is set here; instead we
    return a single-use reset link for the new user to choose their own. This
    avoids the admin ever handling someone else's password."""
    email = payload.get("email", "")
    try:
        norm = auth._normalize_email(email)
    except Exception:
        raise HTTPException(400, "Invalid email address.")
    if auth.get_user_by_email(norm):
        raise HTTPException(409, "A user with that email already exists.")
    # Random unusable password; the reset link is how they set a real one.
    import secrets as _secrets
    uid = auth.create_user(norm, _secrets.token_urlsafe(24) + "Aa1!")
    if payload.get("approved"):
        auth.set_approved(norm, True)
    if payload.get("is_admin"):
        auth.set_admin(norm, True)
    if payload.get("opus_allowed"):
        auth.set_opus_allowed(norm, True)
    token = auth.create_reset_token(uid)
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not base and request is not None:
        scheme = "https" if auth._is_secure(request) else request.url.scheme
        base = f"{scheme}://{request.headers.get('host', '')}"
    return {"ok": True, "reset_url": f"{base}/reset?token={quote(token)}"}


app.include_router(admin_router)


@app.get("/admin")
def admin_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    if not user["is_admin"]:
        # Approved non-admins get bounced to the app rather than the panel.
        return RedirectResponse("/", status_code=302)
