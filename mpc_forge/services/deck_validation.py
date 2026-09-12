"""Validación de mazos por formato.

Commander (EDH): 100 cartas exactas = commander(s) + mainboard.
    - Companion se cuenta APARTE (10ª carta oficial fuera del mazo de 100).
    - Sideboard/maybeboard/tokens NO cuentan.

Además del tamaño se comprueba la **legalidad** de cada carta: Scryfall
publica el estado por formato de cada impresión y lo cacheamos en
``PrintingCache.legalities``, así que detectar un baneado no cuesta ninguna
petición de red.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class IllegalCard:
    """Una carta que no se puede jugar en el formato del mazo."""
    name: str
    status: str   # 'banned' | 'restricted' | 'not_legal'
    role: str


@dataclass
class DeckValidationResult:
    format: str
    expected: int
    counted: int
    is_valid: bool
    message: str
    level: str  # 'ok' | 'warn' | 'error'
    breakdown: dict[str, int]
    illegal: list[IllegalCard] = field(default_factory=list)
    """Cartas baneadas, restringidas o no legales en el formato.

    Vacío también cuando simplemente no tenemos el dato: una carta cuyo
    printing aún no está cacheado no se marca como ilegal. Es deliberado —
    un falso positivo aquí haría dudar al usuario de un mazo correcto, que es
    peor que no avisar.
    """


# Roles que cuentan para el "tamaño oficial" del mazo por formato.
# Todo lo demás (companion, sideboard, maybeboard, tokens) queda fuera.
_COUNTING_ROLES_BY_FORMAT: dict[str, set[str]] = {
    "commander": {"commander", "mainboard"},
    "oathbreaker": {"commander", "mainboard"},  # 60 (Oathbreaker + Signature + 58)
    "brawl": {"commander", "mainboard"},
    "standard": {"mainboard"},
    "modern": {"mainboard"},
    "legacy": {"mainboard"},
    "vintage": {"mainboard"},
    "pioneer": {"mainboard"},
    "pauper": {"mainboard"},
}

_EXPECTED_BY_FORMAT: dict[str, int] = {
    "commander": 100,
    "oathbreaker": 60,
    "brawl": 60,
    "standard": 60,
    "modern": 60,
    "legacy": 60,
    "vintage": 60,
    "pioneer": 60,
    "pauper": 60,
}


# Estados de Scryfall que impiden jugar la carta. `restricted` (Vintage) sí
# permite jugarla, pero solo una copia: se reporta aparte porque el usuario
# necesita saberlo aunque no sea un error de legalidad.
_BLOCKING_STATUSES = {"banned", "not_legal"}
_REPORTED_STATUSES = _BLOCKING_STATUSES | {"restricted"}

# Roles cuyas cartas se comprueban. Maybeboard queda fuera: es una lista de
# ideas, no parte del mazo.
_LEGALITY_ROLES = {"commander", "mainboard", "sideboard", "companion"}


def check_legalities(
    fmt: str,
    cards: Iterable[tuple[str, str, str, bool]],
) -> list[IllegalCard]:
    """Cartas del mazo que no son legales en el formato.

    Args:
        fmt: nombre del formato (case-insensitive).
        cards: iterable de ``(nombre, role, legalities_json, include)``.
            ``legalities_json`` es el JSON tal cual lo cachea
            ``PrintingCache.legalities``; una cadena vacía significa que aún
            no tenemos el dato.
    """
    fmt = (fmt or "").lower().strip()
    if not fmt:
        return []

    out: list[IllegalCard] = []
    seen: set[str] = set()
    for name, role, legalities_json, include in cards:
        if not include or role not in _LEGALITY_ROLES:
            continue
        if not legalities_json or name in seen:
            continue
        try:
            legalities = json.loads(legalities_json)
        except (ValueError, TypeError):
            # Un JSON corrupto en cache no debe romper la vista del mazo.
            log.debug("Legalidades ilegibles para %r", name)
            continue
        status = legalities.get(fmt)
        if status in _REPORTED_STATUSES:
            seen.add(name)
            out.append(IllegalCard(name=name, status=status, role=role))
    out.sort(key=lambda c: (c.status, c.name))
    return out


def validate_deck(
    fmt: str,
    cards: Iterable[tuple[str, int, bool]],
    illegal: list[IllegalCard] | None = None,
) -> DeckValidationResult:
    """
    Args:
        fmt: nombre del formato (case-insensitive)
        cards: iterable de (role, quantity, include) por cada DeckCard
        illegal: resultado de :func:`check_legalities`, si se ha calculado.
            Es un parámetro aparte porque requiere datos (las legalidades
            cacheadas) que no todos los llamantes tienen a mano.
    """
    fmt = (fmt or "commander").lower().strip()
    illegal = illegal or []
    breakdown: dict[str, int] = {}
    for role, qty, include in cards:
        if not include:
            continue
        breakdown[role] = breakdown.get(role, 0) + qty

    if fmt not in _EXPECTED_BY_FORMAT:
        return DeckValidationResult(
            format=fmt,
            expected=0,
            counted=sum(breakdown.values()),
            is_valid=not _blocking(illegal),
            message=_compose_message(f"Formato «{fmt}» sin regla de tamaño", illegal),
            level=_level("ok", illegal),
            breakdown=breakdown,
            illegal=illegal,
        )

    counting = _COUNTING_ROLES_BY_FORMAT.get(fmt, {"mainboard"})
    counted = sum(v for r, v in breakdown.items() if r in counting)
    expected = _EXPECTED_BY_FORMAT[fmt]

    if counted == expected:
        return DeckValidationResult(
            format=fmt,
            expected=expected,
            counted=counted,
            is_valid=not _blocking(illegal),
            message=_compose_message(f"OK · {counted}/{expected} cartas", illegal),
            level=_level("ok", illegal),
            breakdown=breakdown,
            illegal=illegal,
        )
    if counted < expected:
        diff = expected - counted
        return DeckValidationResult(
            format=fmt,
            expected=expected,
            counted=counted,
            is_valid=False,
            message=_compose_message(
                f"Faltan {diff} cartas ({counted}/{expected})", illegal
            ),
            level=_level("warn", illegal),
            breakdown=breakdown,
            illegal=illegal,
        )
    diff = counted - expected
    return DeckValidationResult(
        format=fmt,
        expected=expected,
        counted=counted,
        is_valid=False,
        message=_compose_message(
            f"Sobran {diff} cartas ({counted}/{expected})", illegal
        ),
        level=_level("warn", illegal),
        breakdown=breakdown,
        illegal=illegal,
    )


def _blocking(illegal: list[IllegalCard]) -> bool:
    """¿Hay alguna carta que directamente no se puede jugar?"""
    return any(c.status in _BLOCKING_STATUSES for c in illegal)


def _level(base: str, illegal: list[IllegalCard]) -> str:
    """Una carta baneada es un error, no un aviso: el mazo no es jugable.

    Una restringida solo sube a 'warn', porque sí se puede jugar con una copia.
    """
    if _blocking(illegal):
        return "error"
    if illegal:
        return "warn" if base == "ok" else base
    return base


def _compose_message(base: str, illegal: list[IllegalCard]) -> str:
    if not illegal:
        return base
    blocking = [c for c in illegal if c.status in _BLOCKING_STATUSES]
    if blocking:
        noun = "carta no legal" if len(blocking) == 1 else "cartas no legales"
        return f"{base} · {len(blocking)} {noun}"
    noun = "restringida" if len(illegal) == 1 else "restringidas"
    return f"{base} · {len(illegal)} {noun}"
