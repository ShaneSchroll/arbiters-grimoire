# Live card lookup via the Scryfall API (api.scryfall.com).

import httpx

SCRYFALL = "https://api.scryfall.com"

def lookup_card(name: str) -> dict:
    """
    Fuzzy-match a card by name and return the fields a rules assistant needs.
    Returns {"error": ...} if no card matches, so Claude can react gracefully.
    """
    try:
        r = httpx.get(
            f"{SCRYFALL}/cards/named",
            params={"fuzzy": name},
            timeout=10,
            headers={"User-Agent": "mtg-rules-assistant/1.0"},
        )
    except httpx.RequestError as e:
        return {"error": f"network error contacting Scryfall: {e}"}

    if r.status_code == 404:
        return {"error": f"no card found matching '{name}'"}
    if r.status_code != 200:
        return {"error": f"Scryfall returned HTTP {r.status_code}"}

    c = r.json()

    def faces(card):
        # Double-faced cards keep their text on card_faces instead of the root.
        if "card_faces" in card:
            return card["card_faces"]
        return [card]

    return {
        "name": c.get("name"),
        "mana_cost": c.get("mana_cost"),
        "cmc": c.get("cmc"),
        "type_line": c.get("type_line"),
        "oracle_text": "\n//\n".join(
            f.get("oracle_text", "") for f in faces(c)
        ),
        "power": c.get("power"),
        "toughness": c.get("toughness"),
        "loyalty": c.get("loyalty"),
        "colors": c.get("colors") or c.get("color_identity"),
        "keywords": c.get("keywords", []),
        "legalities": {
            fmt: status
            for fmt, status in c.get("legalities", {}).items()
            if status == "legal"
        },
        "scryfall_uri": c.get("scryfall_uri"),
    }


# Tool schema advertised to Claude. Claude decides when to call this.
CARD_TOOL = {
    "name": "lookup_card",
    "description": (
        "Look up an official Magic: The Gathering card by name to get its "
        "current Oracle text, mana cost, type line, power/toughness, and "
        "format legality. Use this whenever a question references a specific "
        "card and the exact wording matters for the ruling."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Card name (approximate spelling is fine).",
            }
        },
        "required": ["name"],
    },
}
