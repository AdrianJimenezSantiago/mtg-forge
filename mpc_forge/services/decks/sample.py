from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import Deck
from mpc_forge.services.decks import importer

SAMPLE_DECKLIST = """Commander
1 Atraxa, Praetors' Voice

Deck
1 Sol Ring
1 Arcane Signet
1 Command Tower
1 Exotic Orchard
1 Swords to Plowshares
1 Counterspell
1 Cultivate
1 Rhystic Study
1 Smothering Tithe
1 Doubling Season
1 Deepglow Skate
1 Evolution Sage
1 Delver of Secrets
1 Plains
1 Island
1 Swamp
1 Forest
"""


async def create_sample_deck(
    db: AsyncSession, scryfall: ScryfallClient, name: str
) -> tuple[Deck, list[dict[str, Any]]]:
    return await importer.import_from_plaintext(
        db, scryfall, name=name, text=SAMPLE_DECKLIST, fmt="commander"
    )
