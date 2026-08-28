"""Serializa un mazo a texto plano en varios formatos compatibles.

Formatos soportados:
- ``simple``: ``1 Sol Ring`` por línea. Compatible con MTGPrint, MPCFill y
  cualquier importador básico. Sin códigos de set: útil cuando solo importa
  el nombre.
- ``with_set``: ``1 Sol Ring (C21) 263``. Incluye set y collector number, con
  lo que la impresión concreta se conserva al reimportar (Moxfield, MTGO,
  MPCFill).
- ``arena``: ``1 Sol Ring (C21) 263`` con el set en MAYÚSCULAS, formato oficial
  de MTG Arena. Ojo: Arena no reconoce todas las expansiones (promos, etc.);
  en la práctica pierde algunas cartas — es limitación del cliente, no nuestra.

Todos los formatos agrupan por rol con cabeceras separadoras cuando ``include_headers``
es True (default). Al pegar en Moxfield o MPCFill, esto hace que el commander
vaya a "commanders" y el resto a "mainboard" automáticamente.

Solo se serializan cartas con ``include=True``. Los tokens y meld_result se
omiten porque no forman parte de la lista importable (se generan automáticamente
al re-importar en cualquier herramienta seria).
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import DeckCard, PrintingCache

Format = Literal["simple", "with_set", "arena"]


# Orden estable de roles al serializar. Los roles no listados (tokens,
# meld_result, ...) se omiten porque no cuentan como parte importable del mazo.
_ROLE_ORDER: list[tuple[str, str]] = [
    ("commander", "Commander"),
    ("mainboard", "Deck"),
    ("companion", "Companion"),
    ("sideboard", "Sideboard"),
    ("maybeboard", "Maybeboard"),
]


async def build_decklist_text(
    db: AsyncSession,
    deck_id: int,
    fmt: Format = "with_set",
    include_headers: bool = True,
) -> str:
    """Devuelve el texto plano del mazo listo para copiar al portapapeles."""
    # Cartas + su printing en un solo join. deck_id está indexado, así que
    # es una única query pequeña.
    rows = (
        await db.execute(
            select(DeckCard, PrintingCache)
            .join(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id, isouter=True)
            .where(DeckCard.deck_id == deck_id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()

    if not rows:
        return ""

    # Agrupar por rol para poder poner cabeceras en el orden estable.
    by_role: dict[str, list[tuple[DeckCard, PrintingCache | None]]] = {}
    for dc, printing in rows:
        by_role.setdefault(dc.role, []).append((dc, printing))

    lines: list[str] = []
    for role_key, role_label in _ROLE_ORDER:
        group = by_role.get(role_key)
        if not group:
            continue
        if include_headers:
            if lines:
                lines.append("")  # blank line separator entre secciones
            lines.append(role_label)
        for dc, printing in group:
            lines.append(_format_line(dc, printing, fmt))

    return "\n".join(lines) + "\n"


def _format_line(dc: DeckCard, printing: PrintingCache | None, fmt: Format) -> str:
    """Serializa una entrada según el formato pedido."""
    name = dc.name
    qty = dc.quantity

    if fmt == "simple" or printing is None:
        return f"{qty} {name}"

    set_code = printing.set_code or ""
    number = printing.collector_number or ""

    if not set_code or not number:
        # Sin datos de printing → cae a simple
        return f"{qty} {name}"

    if fmt == "arena":
        # Arena espera el set en mayúsculas
        return f"{qty} {name} ({set_code.upper()}) {number}"

    # with_set (MTGO/MPCFill): set en minúsculas
    return f"{qty} {name} ({set_code.lower()}) {number}"


def filename_for(deck_name: str, fmt: Format) -> str:
    """Nombre sugerido para el fichero .txt cuando el usuario elige descargar."""
    from slugify import slugify
    slug = slugify(deck_name) or "deck"
    return f"{slug}-{fmt}.txt"
