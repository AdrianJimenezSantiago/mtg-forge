"""Historial de tiradas de impresión y estadísticas por carta."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckCard, PrintRun, PrintRunItem


@dataclass
class CardHistoryStat:
    oracle_id: str
    card_name: str
    total_copies: int  # copias totales impresas hasta ahora
    times_in_runs: int  # nº de tiradas donde aparece
    last_printed_at: datetime | None
    decks: list[str]  # nombres de mazos donde ha aparecido


async def create_print_run_from_deck(
    db: AsyncSession,
    deck: Deck,
    cardstock: str,
    foil: bool,
    tier_size: int,
    estimated_cost_eur: float,
    xml_path: str | None,
    notes: str | None = None,
    run_name: str | None = None,
) -> PrintRun:
    cards = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id, DeckCard.include.is_(True)
            )
        )
    ).all()
    total = sum(c.quantity for c in cards)
    run = PrintRun(
        name=run_name or f"{deck.name} — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        cardstock=cardstock,
        foil=foil,
        total_cards=total,
        tier_size=tier_size,
        estimated_cost_eur=estimated_cost_eur,
        xml_path=xml_path,
        notes=notes,
    )
    db.add(run)
    await db.flush()
    for c in cards:
        db.add(
            PrintRunItem(
                run_id=run.id,
                deck_id=deck.id,
                deck_name=deck.name,
                scryfall_id=c.scryfall_id,
                oracle_id=c.oracle_id,
                card_name=c.name,
                quantity=c.quantity,
            )
        )
    await db.commit()
    return run


async def stats_for_oracle_ids(
    db: AsyncSession, oracle_ids: list[str]
) -> dict[str, CardHistoryStat]:
    """Para cada oracle_id, cuántas copias se han impreso y en qué mazos.

    Perfecto para "Commander-aware": informativo, no restrictivo.
    """
    if not oracle_ids:
        return {}
    rows = (
        await db.execute(
            select(
                PrintRunItem.oracle_id,
                PrintRunItem.card_name,
                func.sum(PrintRunItem.quantity),
                func.count(PrintRunItem.id),
                func.max(PrintRun.created_at),
            )
            .join(PrintRun, PrintRun.id == PrintRunItem.run_id)
            .where(PrintRunItem.oracle_id.in_(oracle_ids))
            .group_by(PrintRunItem.oracle_id, PrintRunItem.card_name)
        )
    ).all()
    decks_rows = (
        await db.execute(
            select(PrintRunItem.oracle_id, PrintRunItem.deck_name)
            .where(PrintRunItem.oracle_id.in_(oracle_ids))
            .distinct()
        )
    ).all()
    decks_by_oracle: dict[str, list[str]] = {}
    for oid, dn in decks_rows:
        decks_by_oracle.setdefault(oid, []).append(dn)

    return {
        oid: CardHistoryStat(
            oracle_id=oid,
            card_name=name,
            total_copies=int(total or 0),
            times_in_runs=int(runs or 0),
            last_printed_at=last,
            decks=decks_by_oracle.get(oid, []),
        )
        for oid, name, total, runs, last in rows
    }


async def last_scryfall_id_used(
    db: AsyncSession, oracle_id: str
) -> str | None:
    """Devuelve la impresión que se usó en la última tirada para esta carta."""
    stmt = (
        select(PrintRunItem.scryfall_id)
        .join(PrintRun, PrintRun.id == PrintRunItem.run_id)
        .where(PrintRunItem.oracle_id == oracle_id)
        .order_by(PrintRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.first()
    return row[0] if row else None
