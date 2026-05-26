"""
server.py — Web backend for the MTG Rules Oracle.

Flow for each user question:
  1. Retrieve the most relevant rulebook chunks (BM25 over your PDF).
  2. Send them to Claude alongside an MTG-expert system prompt.
  3. If Claude asks to look up a card, call Scryfall and feed the result back.
  4. Return Claude's final answer plus the rule sources used.

Run:  uvicorn server:app --port 8000
Then open http://localhost:8000

All file paths are resolved relative to THIS file, so it does not matter
which folder you launch the command from.
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mtg_api import CARD_TOOL, lookup_card
from retriever import Retriever

BASE_DIR = Path(__file__).resolve().parent

# Load the API key from environment
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


INDEX_HTML = find_index_html()  # fail fast at startup with a clear message

# Models available in the dropdown. Opus is the default for the hardest rules
# interactions; Sonnet is faster and cheaper for everyday questions.
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
exact current Oracle text before ruling — printed wording is often outdated.
- Walk through interactions step by step (priority, the stack, triggered \
abilities, state-based actions) so the player learns the "why".
- Be precise and concise. Distinguish what the rules state from your own \
inference, and flag genuinely ambiguous cases."""

app = FastAPI(title="MTG Rules Oracle")


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
        {"rule": h["rule"] or h["id"], "preview": h["text"][:160]} for h in hits
    ]
    return system, sources


@app.post("/api/chat")
def chat(req: ChatRequest):
    model = req.model if req.model in ALLOWED_MODELS else DEFAULT_MODEL

    last_user = next(
        (m["content"] for m in reversed(req.messages) if m["role"] == "user"),
        "",
    )
    system, sources = build_system(last_user)
    messages = [{"role": m["role"], "content": m["content"]} for m in req.messages]

    # Tool loop: keep calling Claude until it produces a final answer.
    for _ in range(6):  # safety cap on tool round-trips
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=[CARD_TOOL],
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            answer = "".join(
                b.text for b in resp.content if b.type == "text"
            )
            return {"answer": answer, "sources": sources, "model": model}

        # Run every card lookup Claude requested, then loop back.
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "lookup_card":
                result = lookup_card(block.input.get("name", ""))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    return {
        "answer": "The assistant exceeded the tool-use limit. Please rephrase.",
        "sources": sources,
        "model": model,
    }


@app.get("/")
def index():
    return FileResponse(INDEX_HTML)
