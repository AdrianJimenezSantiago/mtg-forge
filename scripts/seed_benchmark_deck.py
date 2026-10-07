from __future__ import annotations

import asyncio
import sys
from typing import Any

from mpc_forge.db import init_db, session_scope
from mpc_forge.services.cards.printings import upsert_printings
from mpc_forge.services.decks.importer import create_deck_from_entries

TYPES = ("Creature — Elf", "Instant", "Land", "Artifact", "Sorcery", "Enchantment")
COLORS = (["G"], ["U"], [], [], ["R"], ["W"])


def printing(index: int, *, type_line: str, colors: list[str]) -> dict[str, Any]:
    sid = f"bench-{index}"
    return {
        "id": sid,
        "oracle_id": f"oracle-{sid}",
        "name": f"Benchmark Card {index}",
        "set": "bch",
        "set_name": "Benchmark",
        "collector_number": str(index),
        "rarity": ("common", "uncommon", "rare", "mythic")[index % 4],
        "lang": "en",
        "layout": "normal",
        "type_line": type_line,
        "mana_cost": "{1}{G}",
        "cmc": float(index % 7),
        "colors": colors,
        "color_identity": colors,
        "image_uris": {"normal": "/static/img/logo.png"},
        "artist": "Benchmark",
        "released_at": "2024-01-01",
        "finishes": ["nonfoil"],
        "prices": {"eur": "0.10"},
        "legalities": {"commander": "legal"},
    }


async def seed(size: int) -> int:
    await init_db()
    cards = [
        printing(i, type_line=TYPES[i % len(TYPES)], colors=COLORS[i % len(COLORS)])
        for i in range(size)
    ]
    async with session_scope() as db:
        await upsert_printings(db, cards)
        await db.commit()
        entries = [
            {
                "name": c["name"],
                "quantity": 1,
                "scryfall_id": c["id"],
                "oracle_id": c["oracle_id"],
                "resolved": True,
                "role": "mainboard",
            }
            for c in cards
        ]
        deck = await create_deck_from_entries(db, f"Benchmark {size}", entries)
        return deck.id


if __name__ == "__main__":
    print(asyncio.run(seed(int(sys.argv[1]) if len(sys.argv) > 1 else 300)))
