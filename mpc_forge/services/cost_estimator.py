"""Estimador de coste MPC en USD y EUR con shipping incluido.

Los precios base de MPC son en USD. Convertimos a EUR y añadimos el envío
(base internacional + extra europeo) para dar el total realista al usuario.

Los valores (tipo de cambio, shipping, tiers) se leen de `mpc_forge.config`
en cada llamada, así los ajustes runtime que el usuario cambie desde
`/settings` se aplican inmediatamente sin reiniciar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mpc_forge import config as cfg


@dataclass
class TierEstimate:
    total_cards: int
    tier_size: int
    unit_usd: float
    subtotal_usd: float
    per_card_effective_usd: float

    # Conversiones EUR + shipping
    subtotal_eur: float = 0.0
    shipping_eur: float = 0.0
    shipping_base_eur: float = 0.0
    shipping_eu_extra_eur: float = 0.0
    total_eur: float = 0.0
    per_card_effective_eur: float = 0.0

    # Sugerencia siguiente tier
    next_tier_size: int | None = None
    cards_to_next_tier: int | None = None
    next_tier_subtotal_usd: float | None = None
    next_tier_subtotal_eur: float | None = None
    next_tier_total_eur: float | None = None
    next_tier_saves_eur: float | None = None


def estimate(
    total_cards: int,
    tiers: list[dict[str, float]] | None = None,
    include_eu_shipping: bool = True,
) -> TierEstimate:
    tiers = tiers or cfg.MPC_TIERS
    tiers_sorted = sorted(tiers, key=lambda t: t["size"])

    tier = next((t for t in tiers_sorted if int(t["size"]) >= total_cards), tiers_sorted[-1])
    tier_size = int(tier["size"])
    unit_usd = float(tier["unit_usd"])
    subtotal_usd = tier_size * unit_usd
    per_card_usd = subtotal_usd / max(total_cards, 1)

    subtotal_eur = subtotal_usd * cfg.USD_TO_EUR
    shipping = cfg.SHIPPING_BASE_EUR + (cfg.SHIPPING_EU_EXTRA_EUR if include_eu_shipping else 0.0)
    total_eur = subtotal_eur + shipping
    per_card_eur = total_eur / max(total_cards, 1)

    # Siguiente tier
    next_tier: dict[str, float] | None = None
    for t in tiers_sorted:
        if int(t["size"]) > tier_size:
            next_tier = t
            break

    if next_tier is None:
        return TierEstimate(
            total_cards=total_cards,
            tier_size=tier_size,
            unit_usd=unit_usd,
            subtotal_usd=round(subtotal_usd, 2),
            per_card_effective_usd=round(per_card_usd, 3),
            subtotal_eur=round(subtotal_eur, 2),
            shipping_eur=round(shipping, 2),
            shipping_base_eur=cfg.SHIPPING_BASE_EUR,
            shipping_eu_extra_eur=cfg.SHIPPING_EU_EXTRA_EUR if include_eu_shipping else 0.0,
            total_eur=round(total_eur, 2),
            per_card_effective_eur=round(per_card_eur, 3),
        )

    n_size = int(next_tier["size"])
    n_unit = float(next_tier["unit_usd"])
    n_sub_usd = n_size * n_unit
    n_sub_eur = n_sub_usd * cfg.USD_TO_EUR
    n_total_eur = n_sub_eur + shipping
    cards_to_next = max(n_size - total_cards, 0)
    # Solo ofrecemos si baja el total en EUR (comparación real, con envío):
    saves_eur = total_eur - n_total_eur if n_total_eur < total_eur else None

    return TierEstimate(
        total_cards=total_cards,
        tier_size=tier_size,
        unit_usd=unit_usd,
        subtotal_usd=round(subtotal_usd, 2),
        per_card_effective_usd=round(per_card_usd, 3),
        subtotal_eur=round(subtotal_eur, 2),
        shipping_eur=round(shipping, 2),
        shipping_base_eur=cfg.SHIPPING_BASE_EUR,
        shipping_eu_extra_eur=cfg.SHIPPING_EU_EXTRA_EUR if include_eu_shipping else 0.0,
        total_eur=round(total_eur, 2),
        per_card_effective_eur=round(per_card_eur, 3),
        next_tier_size=n_size,
        cards_to_next_tier=cards_to_next,
        next_tier_subtotal_usd=round(n_sub_usd, 2),
        next_tier_subtotal_eur=round(n_sub_eur, 2),
        next_tier_total_eur=round(n_total_eur, 2),
        next_tier_saves_eur=round(saves_eur, 2) if saves_eur else None,
    )
