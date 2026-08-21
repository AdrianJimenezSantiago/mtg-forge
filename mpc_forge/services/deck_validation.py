"""Validación de mazos por formato.

Commander (EDH): 100 cartas exactas = commander(s) + mainboard.
    - Companion se cuenta APARTE (10ª carta oficial fuera del mazo de 100).
    - Sideboard/maybeboard/tokens NO cuentan.

Otros formatos: no implementados aún; se devuelve estado "unknown".
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass
class DeckValidationResult:
    format: str
    expected: int
    counted: int
    is_valid: bool
    message: str
    level: str  # 'ok' | 'warn' | 'error'
    breakdown: dict[str, int]


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


def validate_deck(
    fmt: str,
    cards: Iterable[tuple[str, int, bool]],
) -> DeckValidationResult:
    """
    Args:
        fmt: nombre del formato (case-insensitive)
        cards: iterable de (role, quantity, include) por cada DeckCard
    """
    fmt = (fmt or "commander").lower().strip()
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
            is_valid=True,
            message=f"Formato «{fmt}» sin regla de tamaño",
            level="ok",
            breakdown=breakdown,
        )

    counting = _COUNTING_ROLES_BY_FORMAT.get(fmt, {"mainboard"})
    counted = sum(v for r, v in breakdown.items() if r in counting)
    expected = _EXPECTED_BY_FORMAT[fmt]

    if counted == expected:
        return DeckValidationResult(
            format=fmt,
            expected=expected,
            counted=counted,
            is_valid=True,
            message=f"OK · {counted}/{expected} cartas",
            level="ok",
            breakdown=breakdown,
        )
    if counted < expected:
        diff = expected - counted
        return DeckValidationResult(
            format=fmt,
            expected=expected,
            counted=counted,
            is_valid=False,
            message=f"Faltan {diff} cartas ({counted}/{expected})",
            level="warn",
            breakdown=breakdown,
        )
    diff = counted - expected
    return DeckValidationResult(
        format=fmt,
        expected=expected,
        counted=counted,
        is_valid=False,
        message=f"Sobran {diff} cartas ({counted}/{expected})",
        level="warn",
        breakdown=breakdown,
    )
