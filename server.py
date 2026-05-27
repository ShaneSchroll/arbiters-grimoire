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

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
TIPS_HTML = BASE_DIR / "tips.html"
AUTH_PAGES = BASE_DIR / "auth_pages"

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


app = FastAPI(title="MTG Rules Oracle", lifespan=lifespan)
app.include_router(auth.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatRequest(BaseModel):
    messages: list  # [{"role": "user"|"assistant", "content": "..."}]
    model: str = DEFAULT_MODEL


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
def chat(req: ChatRequest, _user=Depends(auth.require_user)):
    model = req.model if req.model in ALLOWED_MODELS else DEFAULT_MODEL

    last_user = next(
        (m["content"] for m in reversed(req.messages) if m["role"] == "user"),
        "",
    )
    system, sources = build_system(last_user)
    messages = [{"role": m["role"], "content": m["content"]} for m in req.messages]

    def event_stream():
        """Server-Sent Events: delta chunks during generation, then a final
        'done' event carrying the filtered sources. The tool loop continues
        between streamed rounds - text from "let me look that up" rounds is
        streamed too, so the user sees what's happening live."""
        answer_parts: list[str] = []
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


@app.get("/tips")
def tips_page(request: Request):
    user = auth.get_current_user(request)
    if not user or not user["approved"]:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(TIPS_HTML)


@app.get("/login")
def login_page():
    return FileResponse(AUTH_PAGES / "login.html")


@app.get("/register")
def register_page():
    return FileResponse(AUTH_PAGES / "register.html")


@app.get("/reset")
def reset_page():
    return FileResponse(AUTH_PAGES / "reset.html")
