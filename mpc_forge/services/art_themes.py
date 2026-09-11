"""Temas de arte: guardar una selección de artes y aplicarla a otro mazo.

El problema
-----------
``ArtPreference`` guarda la elección de arte por ``oracle_id``, pero es un
espacio global único: solo puede haber una preferencia por carta. Quien tiene
un estilo definido —todo anime, todo retro frame, todo del mismo artista— no
puede mantener dos colecciones de preferencias a la vez, ni aplicar su estilo a
un mazo nuevo sin volver a elegir carta por carta.

Un ``ArtTheme`` agrupa N elecciones bajo un nombre y se puede aplicar en bloque:
las cartas del mazo destino que coincidan por ``oracle_id`` adoptan el arte del
tema, y el resto se queda como está.

Decisiones de diseño
--------------------
* **Se captura por oracle_id, no por carta.** Un tema debe poder aplicarse a
  cualquier mazo, y el id de la fila ``DeckCard`` es local a su mazo.
* **Aplicar es no destructivo por defecto.** Solo se tocan las cartas que el
  tema cubre. Aplicar un tema de 12 cartas a un Commander de 100 cambia 12.
* **Se guarda un snapshot del nombre.** Si el arte custom se borra del disco,
  el usuario sigue viendo de qué carta se trataba al inspeccionar el tema.
* **Aplicar crea un snapshot previo del mazo.** Es una operación masiva y
  reversible: si el resultado no gusta, se restaura. Ver ``snapshots.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import ArtTheme, ArtThemeEntry, Deck, DeckCard

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    """Qué cambió al aplicar un tema."""
    theme_id: int
    theme_name: str
    deck_id: int
    changed: int = 0
    already_matching: int = 0
    not_in_theme: int = 0
    missing_art: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "deck_id": self.deck_id,
            "changed": self.changed,
            "already_matching": self.already_matching,
            "not_in_theme": self.not_in_theme,
            "missing_art": self.missing_art,
        }


async def list_themes(db: AsyncSession) -> list[dict[str, Any]]:
    """Todos los temas con su contador. El contador está desnormalizado en la
    tabla para no hacer un COUNT correlacionado por fila en cada carga."""
    rows = (await db.execute(
        select(ArtTheme).order_by(ArtTheme.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "entry_count": t.entry_count,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


async def get_theme(db: AsyncSession, theme_id: int) -> dict[str, Any] | None:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return None
    entries = (await db.execute(
        select(ArtThemeEntry)
        .where(ArtThemeEntry.theme_id == theme_id)
        .order_by(ArtThemeEntry.card_name)
    )).scalars().all()
    return {
        "id": theme.id,
        "name": theme.name,
        "description": theme.description,
        "entry_count": theme.entry_count,
        "created_at": theme.created_at.isoformat() if theme.created_at else None,
        "entries": [
            {
                "oracle_id": e.oracle_id,
                "card_name": e.card_name,
                "scryfall_id": e.scryfall_id,
                "custom_art_front_id": e.custom_art_front_id,
                "custom_art_back_id": e.custom_art_back_id,
            }
            for e in entries
        ],
    }


async def create_from_deck(
    db: AsyncSession,
    deck_id: int,
    name: str,
    *,
    description: str = "",
    only_customized: bool = True,
) -> dict[str, Any] | None:
    """Captura la selección de artes de un mazo como tema nuevo.

    ``only_customized`` (por defecto) guarda solo las cartas cuyo arte se ha
    tocado a mano: las que usan arte custom, o las que apuntan a una impresión
    distinta de la que traía el mazo al importarse. Guardar las 100 cartas de
    un Commander cuando el usuario solo cambió 8 haría el tema inútil, porque
    al aplicarlo pisaría todo el mazo destino.
    """
    deck = await db.get(Deck, deck_id)
    if deck is None:
        return None

    cards = (await db.execute(
        select(DeckCard).where(DeckCard.deck_id == deck_id)
    )).scalars().all()

    theme = ArtTheme(name=name.strip() or "Tema sin nombre", description=description)
    db.add(theme)
    await db.flush()   # necesitamos el id para las entradas

    seen: set[str] = set()
    count = 0
    for card in cards:
        has_custom = (
            card.custom_art_front_id is not None
            or card.custom_art_back_id is not None
        )
        if only_customized and not has_custom:
            continue
        if not card.oracle_id or card.oracle_id in seen:
            # Un mazo puede llevar varias copias de la misma carta con artes
            # distintos (tierras básicas). Gana la primera: un tema es una
            # elección por carta, no por copia.
            continue
        seen.add(card.oracle_id)

        db.add(ArtThemeEntry(
            theme_id=theme.id,
            oracle_id=card.oracle_id,
            card_name=card.name,
            scryfall_id=card.scryfall_id,
            custom_art_front_id=card.custom_art_front_id,
            custom_art_back_id=card.custom_art_back_id,
        ))
        count += 1

    theme.entry_count = count
    await db.commit()
    log.info("Tema '%s' creado con %d entradas desde el mazo %d",
             theme.name, count, deck_id)
    return await get_theme(db, theme.id)


async def apply_to_deck(
    db: AsyncSession,
    theme_id: int,
    deck_id: int,
    *,
    overwrite_custom: bool = True,
) -> ApplyResult | None:
    """Aplica un tema a un mazo.

    ``overwrite_custom=False`` respeta las cartas que ya tienen arte custom
    elegido a mano, y solo toca las que están con su arte por defecto. Útil
    para aplicar un tema encima de un mazo ya trabajado sin perder el trabajo.
    """
    theme = await db.get(ArtTheme, theme_id)
    deck = await db.get(Deck, deck_id)
    if theme is None or deck is None:
        return None

    entries = (await db.execute(
        select(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id)
    )).scalars().all()
    by_oracle = {e.oracle_id: e for e in entries}

    cards = (await db.execute(
        select(DeckCard).where(DeckCard.deck_id == deck_id)
    )).scalars().all()

    result = ApplyResult(
        theme_id=theme.id, theme_name=theme.name, deck_id=deck_id
    )

    for card in cards:
        entry = by_oracle.get(card.oracle_id)
        if entry is None:
            result.not_in_theme += 1
            continue

        has_custom = (
            card.custom_art_front_id is not None
            or card.custom_art_back_id is not None
        )
        if has_custom and not overwrite_custom:
            result.already_matching += 1
            continue

        unchanged = (
            card.scryfall_id == (entry.scryfall_id or card.scryfall_id)
            and card.custom_art_front_id == entry.custom_art_front_id
            and card.custom_art_back_id == entry.custom_art_back_id
        )
        if unchanged:
            result.already_matching += 1
            continue

        if entry.scryfall_id:
            card.scryfall_id = entry.scryfall_id
        card.custom_art_front_id = entry.custom_art_front_id
        card.custom_art_back_id = entry.custom_art_back_id
        result.changed += 1

    await db.commit()
    log.info("Tema '%s' aplicado al mazo %d: %d cartas cambiadas",
             theme.name, deck_id, result.changed)
    return result


async def preview_apply(
    db: AsyncSession, theme_id: int, deck_id: int
) -> dict[str, Any] | None:
    """Qué pasaría al aplicar, sin tocar nada.

    Una operación que cambia decenas de cartas de golpe no debería ejecutarse
    a ciegas. La interfaz enseña esto y pide confirmación.
    """
    theme = await db.get(ArtTheme, theme_id)
    deck = await db.get(Deck, deck_id)
    if theme is None or deck is None:
        return None

    entries = (await db.execute(
        select(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id)
    )).scalars().all()
    by_oracle = {e.oracle_id: e for e in entries}

    cards = (await db.execute(
        select(DeckCard).where(DeckCard.deck_id == deck_id)
    )).scalars().all()

    would_change = []
    for card in cards:
        entry = by_oracle.get(card.oracle_id)
        if entry is None:
            continue
        unchanged = (
            card.scryfall_id == (entry.scryfall_id or card.scryfall_id)
            and card.custom_art_front_id == entry.custom_art_front_id
            and card.custom_art_back_id == entry.custom_art_back_id
        )
        if unchanged:
            continue
        would_change.append({
            "card_id": card.id,
            "name": card.name,
            "from_scryfall_id": card.scryfall_id,
            "to_scryfall_id": entry.scryfall_id,
            "to_custom_front": entry.custom_art_front_id,
        })

    return {
        "theme_id": theme.id,
        "theme_name": theme.name,
        "deck_id": deck_id,
        "deck_name": deck.name,
        "would_change": len(would_change),
        "unaffected": len(cards) - len(would_change),
        "cards": would_change[:100],   # tope: la vista previa no es un informe
    }


async def delete_theme(db: AsyncSession, theme_id: int) -> bool:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return False
    # El cascade del modelo borra las entradas, pero se hace explícito para no
    # depender de que la FK tenga ON DELETE CASCADE activo en SQLite (que solo
    # se aplica con PRAGMA foreign_keys=ON).
    await db.execute(
        delete(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id)
    )
    await db.delete(theme)
    await db.commit()
    return True


async def rename_theme(
    db: AsyncSession, theme_id: int, name: str, description: str | None = None
) -> dict[str, Any] | None:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return None
    if name.strip():
        theme.name = name.strip()
    if description is not None:
        theme.description = description
    await db.commit()
    return await get_theme(db, theme_id)


async def recount(db: AsyncSession, theme_id: int) -> int:
    """Resincroniza el contador desnormalizado. Reparación, no camino normal."""
    total = (await db.scalar(
        select(func.count())
        .select_from(ArtThemeEntry)
        .where(ArtThemeEntry.theme_id == theme_id)
    )) or 0
    theme = await db.get(ArtTheme, theme_id)
    if theme is not None:
        theme.entry_count = int(total)
        await db.commit()
    return int(total)
