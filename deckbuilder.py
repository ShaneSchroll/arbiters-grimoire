"""
deckbuilder.py — Backend for the AI Deck Builder page.

Flow per request:
  1. Resolve every entered card against the local cache (network only on a true
     miss, memoized — see mtg_api.lookup_card).
  2. Build a system prompt embedding the resolved decklist as ground truth.
  3. Stream Claude's suggestions (adds / cuts) over SSE, giving Claude the same
     lookup_card tool so it can verify any card it wants to recommend.

server.py wires this up with:
    import deckbuilder
    deckbuilder.configure(client, ALLOWED_MODELS, DEFAULT_MODEL)
    app.include_router(deckbuilder.router)
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import auth
from mtg_api import CARD_TOOL, get_cache, lookup_card

router = APIRouter(prefix="/api", tags=["deckbuilder"])

# Injected by server.configure() so we share one client + one model policy.
_client = None
_allowed_models: set[str] = set()
_default_model = "claude-opus-4-8"
_model_call_params: dict = {}


def configure(client, allowed_models: set[str], default_model: str,
              model_call_params: dict | None = None) -> None:
    global _client, _allowed_models, _default_model, _model_call_params
    _client = client
    _allowed_models = set(allowed_models)
    _default_model = default_model
    _model_call_params = dict(model_call_params or {})


# ---- Input model (mirrors server.ChatRequest's caps) -----------------------

MAX_DECK_CARDS = 250          # generous: Commander is 100, with a sideboard buffer
MAX_NAME_CHARS = 120
MAX_NOTES_CHARS = 1_000
MAX_TURNS = 12                # allow iterative refinement ("make it more aggressive")


class DeckCard(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    count: int = Field(default=1, ge=1, le=99)


class DeckTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8_000)


class DeckRequest(BaseModel):
    deck: list[DeckCard] = Field(default_factory=list, max_length=MAX_DECK_CARDS)
    fmt: str = Field(default="", max_length=40)          # e.g. "Commander", "Modern"
    notes: str = Field(default="", max_length=MAX_NOTES_CHARS)  # the player's goal
    messages: list[DeckTurn] = Field(default_factory=list, max_length=MAX_TURNS)
    model: str = _default_model


SYSTEM_PERSONA = """You are the Oracle's Deck Builder, an expert Magic: The \
Gathering deckbuilding coach.

You are given the player's current decklist with each card's real, current \
Oracle text, mana value, type, colors, and keywords (already resolved for you \
below). Treat that as authoritative ground truth.

Your job, based on the player's stated format and goal:
- ADD: recommend specific cards that would help finish or strengthen the deck \
(fixing the mana curve, filling a role the deck lacks, improving consistency, \
or pushing the deck's plan). Name real cards. Before recommending a card whose \
exact text matters, use the lookup_card tool to confirm its current wording and \
legality in the stated format.
- CUT: identify the weakest current cards and explain what to remove and why \
(off-plan, redundant, too slow, wrong colors, illegal in the format). Keep the explanation short.
- Briefly note the deck's apparent archetype, color identity, and curve so the \
player learns the "why", not just a list.

Be concrete and concise. Prefer cards legal in the stated format. If the goal \
is unclear, state the most reasonable assumption and proceed. Group your answer \
under clear 'Add' and 'Cut' sections."""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _resolve_deck(deck: list[DeckCard]) -> tuple[str, list[str]]:
    """Resolve entered names against the shared cache. Returns a compact,
    Claude-readable decklist block plus a list of names that couldn't be
    resolved (so the UI/Claude can flag typos)."""
    lines: list[str] = []
    unresolved: list[str] = []
    for entry in deck:
        card = lookup_card(entry.name)  # cache-first; memoized live fallback
        if not card or "error" in card:
            unresolved.append(entry.name)
            continue
        legal = ", ".join(sorted(card.get("legalities", {}).keys())) or "—"
        lines.append(
            f"{entry.count}x {card['name']} | {card.get('mana_cost') or '—'} "
            f"(MV {card.get('cmc')}) | {card.get('type_line') or '—'} | "
            f"colors={card.get('color_identity') or []} | legal: {legal}\n"
            f"    {(card.get('oracle_text') or '').replace(chr(10), ' ')}"
        )
    block = "\n".join(lines) if lines else "(empty decklist)"
    return block, unresolved


def _build_system(req: DeckRequest) -> tuple[list[dict], list[str]]:
    deck_block, unresolved = _resolve_deck(req.deck)
    context = (
        f"FORMAT: {req.fmt or 'unspecified'}\n"
        f"PLAYER'S GOAL: {req.notes or 'unspecified'}\n\n"
        f"CURRENT DECKLIST (resolved):\n{deck_block}"
    )
    if unresolved:
        context += "\n\nNOTE: these entered names did not resolve and were skipped: " \
                    + ", ".join(unresolved)
    system = [
        {"type": "text", "text": SYSTEM_PERSONA, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": context},
    ]
    return system, unresolved


@router.post("/deckbuilder")
def deckbuilder(req: DeckRequest, request: Request, user=Depends(auth.require_user)):
    # Same-origin as defense-in-depth, matching /api/chat: both spend API dollars.
    auth.require_same_origin(request)
    if _client is None:
        raise HTTPException(503, "Deck builder is not configured.")
    if auth.chat_rate_limited(user["id"]):
        raise HTTPException(429, "Rate limit exceeded. Please wait a moment and try again.")
    if auth.daily_budget_exceeded(user["id"]):
        raise HTTPException(429, "Daily usage budget reached. It resets at 00:00 UTC.")
    if not get_cache_safe():
        raise HTTPException(503, "Card cache is missing. Run `python build_card_cache.py`.")

    # Any allowed model is available to every user; unknown models fall back.
    model = req.model if req.model in _allowed_models else _default_model

    system, _ = _build_system(req)

    # Conversation history (for iterative refinement). The deck context lives in
    # the system prompt, so the first turn can be a simple instruction.
    if req.messages:
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
    else:
        messages = [{"role": "user",
                     "content": "Analyze my deck. What should I add to finish it, "
                                "and what should I cut?"}]

    def event_stream():
        # Mirrors /api/chat's stream+tool loop and per-round usage metering.
        answer_parts: list[str] = []
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
                with _client.messages.stream(
                    model=model,
                    max_tokens=8192,
                    system=system,
                    tools=[CARD_TOOL],
                    messages=messages,
                    **_model_call_params.get(model, {}),
                ) as stream:
                    for chunk in stream.text_stream:
                        answer_parts.append(chunk)
                        yield _sse({"type": "delta", "text": chunk})
                    final = stream.get_final_message()
                add_usage(getattr(final, "usage", None))

                if final.stop_reason != "tool_use":
                    yield _sse({"type": "done", "model": model})
                    return

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

            yield _sse({"type": "error",
                        "message": "Exceeded the tool-use limit. Please try again."})
        except Exception:
            yield _sse({"type": "error", "message": "Server error while generating."})
        finally:
            if any(usage.values()):
                try:
                    auth.record_usage(user["id"], model, usage["input"],
                                      usage["output"], usage["cache_write"],
                                      usage["cache_read"])
                except Exception:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def get_cache_safe() -> bool:
    try:
        get_cache()
        return True
    except FileNotFoundError:
        return False
