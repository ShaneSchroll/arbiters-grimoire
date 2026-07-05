"""
server.py - Web backend for the Arbiters Grimoire.

Flow for each user question:
  1. Retrieve the most relevant rulebook chunks (BM25 over your TXT).
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
import card_refresh
import deckbuilder
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

# Exact per-subrule text index for the citation popover. rules.json chunks are
# keyed by parent rule (e.g. "510.1"), with subrules ("510.1c") living inside
# the chunk text - so a citation like 510.1c isn't directly addressable there.
# We flatten every numbered line into {number -> paragraph text} once at startup
# so /api/rule/<id> can return the exact text a citation points at.
_RULE_LINE = re.compile(r"^(\d{3}\.\d+[a-z]?)\.?\s+(.*)$")


def _build_rule_index(chunks) -> dict[str, str]:
    idx: dict[str, str] = {}
    for chunk in chunks:
        current = None
        for raw in chunk["text"].split("\n"):
            line = raw.strip()
            if not line:
                continue
            m = _RULE_LINE.match(line)
            if m:
                current = m.group(1)
                idx[current] = m.group(2).strip()
            elif current:
                # Continuation / "Example:" line belongs to the current subrule.
                idx[current] += " " + line
    return idx


RULE_INDEX = _build_rule_index(retriever.chunks)


# ---------------------------------------------------------------------------
# Frontend: served straight out of Astro's build output (dist/).
#
# `npm run build` emits one folder per page (dist/<route>/index.html), hashed
# JS/CSS into dist/_astro/, and copies public/ assets (favicons) to the dist
# root. The routes below repoint the app's existing URLs at those built files,
# replacing the hand-written HTML + /static layout used before the migration.
# Public URLs are intentionally unchanged - only the file each one serves moved.
# ---------------------------------------------------------------------------
DIST_DIR = BASE_DIR / "dist"
ASTRO_ASSETS = DIST_DIR / "_astro"
DOCS_JSON = BASE_DIR / "docs.json"


def _load_cr_effective_date() -> str | None:
    """The Comprehensive Rules' effective date, captured into docs.json by
    ingest.py. Read once at startup for the CR badge in the chat header; a
    missing/unreadable value is fine — the UI just hides the chip. Refreshes on
    restart, in step with the retriever's rules.json load."""
    try:
        with open(DOCS_JSON, encoding="utf-8") as f:
            date = json.load(f).get("effective_date")
    except (OSError, json.JSONDecodeError):
        return None
    return date if isinstance(date, str) and date.strip() else None


CR_EFFECTIVE_DATE = _load_cr_effective_date()


def _serve_page(rel: str) -> FileResponse:
    """FileResponse for a built page (``rel`` is a path relative to dist/).

    Resolved per request, so editing a page and re-running ``npm run build``
    shows up on the next reload with no server restart. Returns a clear 503 when
    the build is missing.
    """
    page = DIST_DIR / rel
    if page.is_file():
        return FileResponse(page)
    raise HTTPException(
        status_code=503,
        detail="Frontend build missing. Run `npm run build` to generate dist/.",
    )


def _dist_file(rel: str, *, cache: str | None = None) -> FileResponse:
    """Serve a single file from the dist root (e.g. a favicon). 404s when the
    build hasn't produced it, rather than the 503 used for whole pages."""
    f = DIST_DIR / rel
    if not f.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(f, headers={"Cache-Control": cache} if cache else None)

# The one model every signed-in user gets: Claude Opus 4.8. It is both the sole
# option and the global default, with no per-user gating - everyone can use it.
ALLOWED_MODELS = {"claude-opus-4-8"}
DEFAULT_MODEL = "claude-opus-4-8"

# Per-model extra params merged into each Messages API call. Opus 4.8 runs with
# adaptive thinking (the model decides when and how much to think) at high
# reasoning effort.
MODEL_CALL_PARAMS = {
    "claude-opus-4-8": {
        # Adaptive: think on complex queries, answer instantly on simple ones.
        # display "omitted" hides the thought summary (already the Opus 4.8
        # default) - the model still thinks and is billed for it either way,
        # so this is a visibility setting, not a way to save tokens.
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
    },
}

deckbuilder.configure(client, ALLOWED_MODELS, DEFAULT_MODEL, MODEL_CALL_PARAMS)

# Static, cacheable persona. Kept separate from the per-question rules text so
# it can be marked with cache_control and reused cheaply across requests.
SYSTEM_PERSONA = """You are the MTG Arbiter, a meticulous judge-level \
expert on Magic: The Gathering rules and card interactions.

How to reason:
- Reason strictly from the RULEBOOK EXCERPTS provided in the user turn; they \
are authoritative. If they are insufficient, say so plainly rather than guessing.
- When a question names a specific card, use the lookup_card tool to get its \
exact current Oracle text before ruling - printed wording is often outdated.
- Work through the interaction in order (priority, the stack, layers, triggered \
abilities, state-based actions) so the player learns the "why".

OUTPUT FORMAT - follow this EXACTLY and consistently for every ruling:
1. The FIRST line must be the verdict, written as `VERDICT: <one concise \
sentence>` - e.g. `VERDICT: Yes - that damage assignment is legal.` Give a \
direct answer; if it genuinely depends, write `VERDICT: It depends - <the key \
factor>.`
2. Then a blank line, then the explanation as GitHub-flavored Markdown.
3. In the explanation, use a numbered list (`1.`, `2.`, `3.`) for step-by-step \
reasoning. Use **bold** for key rules terms and wrap short game terms in \
`inline code` (e.g. `combat damage step`).
4. Cite the specific Comprehensive Rules number inline, right where it applies, \
as the bare number in parentheses - e.g. (702.2b). Cite the exact subrule that \
governs (702.2b), not just its parent (702.2).
5. Wrap every specific card name in double square brackets so it links to the \
card, e.g. [[Basilisk Collar]]. Bracket only real card names, never rules terms.

Be precise and concise. Distinguish what the rules state from your own \
inference, and flag genuinely ambiguous cases in the verdict."""


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.init_db()
    yield

_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    # Astro pages load the Inter webfont stylesheet + woff2 files from rsms.me.
    "style-src 'self' 'unsafe-inline' https://rsms.me https://fonts.googleapis.com; "
    "font-src 'self' https://rsms.me https://fonts.gstatic.com; "
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


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles for Astro's /_astro bundles. Their filenames are content
    hashed, so a given URL never changes meaning - safe to cache hard. The
    browser refetches only when the hash (and thus the URL) changes."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        return response


# docs_url/redoc_url/openapi_url disabled: the interactive docs would hand
# anonymous visitors the full API map, including every admin route.
app = FastAPI(
    title="Arbiters Grimoire",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth.router)

# Astro's hashed JS/CSS bundles. check_dir=False so the app still boots when
# dist/ hasn't been built yet (the page routes return a clear 503 in that case).
app.mount(
    "/_astro",
    ImmutableStaticFiles(directory=ASTRO_ASSETS, check_dir=False),
    name="astro",
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return _dist_file("favicon.ico", cache="public, max-age=86400")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return _dist_file("favicon.svg", cache="public, max-age=86400")

# Caps on chat input. These bound how large a conversation can get before we
# ask the user to start a new chat - they are NOT model context limits (these
# models have ~1M-token windows). max_tokens (8192) is shared by adaptive
# thinking and the visible answer, but only the visible answer is stored back
# into history - concise rules answers stay well under MAX_MESSAGE_CHARS when
# re-sent; MAX_TOTAL_CHARS allows several turns before the client nudges toward
# a fresh chat (HTTP 422).
MAX_MESSAGES = 15
MAX_MESSAGE_CHARS = 12_000
MAX_TOTAL_CHARS = 48_000


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
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
def chat(req: ChatRequest, request: Request, user=Depends(auth.require_user)):
    # Same-origin as defense-in-depth (matches the conversations/admin routes):
    # this endpoint spends real API dollars, so it gets the same guard even
    # though SameSite=Lax already keeps cross-site POSTs cookie-less.
    auth.require_same_origin(request)
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

    # Every signed-in user may pick any allowed model; an unknown or
    # unsupported model silently falls back to the default rather than erroring.
    model = req.model if req.model in ALLOWED_MODELS else DEFAULT_MODEL

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
                    max_tokens=8192,
                    system=system,
                    tools=[CARD_TOOL],
                    messages=messages,
                    **MODEL_CALL_PARAMS.get(model, {}),
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


@app.get("/api/rule/{rule_id}")
def rule_text(rule_id: str, _user=Depends(auth.require_user)):
    """Exact text for a single Comprehensive Rules citation (e.g. 510.1c), so the
    client citation popover/sheet can show the real rule rather than a fallback."""
    text = RULE_INDEX.get(rule_id.strip())
    if not text:
        raise HTTPException(status_code=404, detail=f"No rule {rule_id}.")
    return {"rule": rule_id, "text": text}


# Longest real card name (an Un-set joke card) is ~141 chars; anything past
# this is garbage and shouldn't reach the cache or the live Scryfall fallback.
MAX_CARD_NAME_CHARS = 200


@app.get("/api/card")
def card_text(name: str, user=Depends(auth.require_user)):
    """Text-only card preview (name, cost, type, Oracle text) for card links in
    answers. Cache-first via mtg_api; no imagery, per the legal constraint."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A card name is required.")
    if len(name) > MAX_CARD_NAME_CHARS:
        raise HTTPException(status_code=400, detail="Card name is too long.")
    if auth.card_rate_limited(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="Too many card lookups. Please wait a moment and try again.",
        )
    hit = lookup_card(name)
    if not hit or hit.get("error"):
        raise HTTPException(status_code=404, detail=f"No card matching '{name}'.")
    return {
        "name": hit.get("name") or name,
        "cost": hit.get("mana_cost") or "",
        "type": hit.get("type_line") or "",
        "text": hit.get("oracle_text") or "",
    }


# ----- Conversation history (sidebar archive; capped per user in auth.py) -----

class ConvMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class ConvSave(BaseModel):
    id: int | None = None
    title: str = Field(min_length=1, max_length=200)
    format: str = Field(default="Commander", max_length=40)
    messages: list[ConvMessage] = Field(min_length=1, max_length=MAX_MESSAGES)


@app.get("/api/conversations")
def conversations_list(user=Depends(auth.require_user)):
    return {"conversations": auth.list_conversations(user["id"])}


@app.post("/api/conversations")
def conversations_save(req: ConvSave, request: Request, user=Depends(auth.require_user)):
    auth.require_same_origin(request)
    messages_json = json.dumps(
        [m.model_dump() for m in req.messages], ensure_ascii=False
    )
    cid = auth.save_conversation(
        user["id"], req.id, req.title.strip()[:200], req.format, messages_json
    )
    return {"id": cid}


@app.get("/api/conversations/{conv_id}")
def conversations_get(conv_id: int, user=Depends(auth.require_user)):
    conv = auth.get_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    conv["messages"] = json.loads(conv["messages"])
    return conv


@app.delete("/api/conversations/{conv_id}")
def conversations_delete(conv_id: int, request: Request, user=Depends(auth.require_user)):
    auth.require_same_origin(request)
    if not auth.delete_conversation(user["id"], conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"ok": True}


@app.get("/")
def index(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return _serve_page("index.html")


# In-app pages from the redesigned nav. Auth-gated like the chat root: these
# are app chrome, not public marketing pages. (These replace the pre-redesign
# /pages/tips and /pages/rules routes, whose dist targets no longer exist.)

@app.get("/help")
def help_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return _serve_page("help/index.html")


@app.get("/rulebook")
def rulebook_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return _serve_page("rulebook/index.html")


@app.get("/about")
def about_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return _serve_page("about/index.html")


@app.get("/docs.json")
def docs_json(_user=Depends(auth.require_user)):
    """Powers the in-app rules docs page. Auth-gated like /api/chat -
    don't ship the cleaned rulebook to anonymous visitors. GZipMiddleware
    above compresses the ~1MB JSON down to ~360KB on the wire."""
    if not DOCS_JSON.exists():
        raise HTTPException(
            status_code=503,
            detail="docs.json is missing. Run `python ingest.py rules.txt` to generate it.",
        )
    return FileResponse(DOCS_JSON, media_type="application/json")


@app.get("/api/cr-version")
def cr_version():
    """Effective date of the loaded Comprehensive Rules, for the CR badge in the
    chat header. Public: it's just non-sensitive version metadata (the date is
    published on WotC's site), so it needn't be auth-gated like the rules text."""
    return {"effective_date": CR_EFFECTIVE_DATE}


@app.get("/login")
def login_page():
    return _serve_page("auth/login/index.html")


@app.get("/register")
def register_page():
    return _serve_page("auth/register/index.html")


@app.get("/reset")
def reset_page():
    return _serve_page("auth/reset/index.html")


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
    token = auth.create_reset_token(uid)
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not base and request is not None:
        scheme = "https" if auth._is_secure(request) else request.url.scheme
        base = f"{scheme}://{request.headers.get('host', '')}"
    return {"ok": True, "reset_url": f"{base}/reset?token={quote(token)}"}


@admin_router.post("/refresh-cards")
def admin_refresh_cards(payload: dict = Body(default={}), _admin=Depends(_admin_guard)):
    # _admin_guard = same-origin + admin, matching your other state-changing routes
    return card_refresh.start_refresh(force=bool(payload.get("force", False)))

@admin_router.get("/refresh-cards/status")
def admin_refresh_cards_status(_admin=Depends(auth.require_admin)):
    return card_refresh.get_status()


app.include_router(admin_router)
app.include_router(deckbuilder.router)

@app.get("/deckbuilder")
def deckbuilder_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return _serve_page("deckbuilder/index.html")


@app.get("/admin")
def admin_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    if not user["is_admin"]:
        # Approved non-admins get bounced to the app rather than the panel.
        return RedirectResponse("/", status_code=302)
    return _serve_page("admin/index.html")