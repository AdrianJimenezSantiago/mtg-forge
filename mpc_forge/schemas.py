"""Pydantic schemas para request/response."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ---- Requests ------------------------------------------------------------

class ImportFromMoxfieldRequest(BaseModel):
    url_or_id: str = Field(..., min_length=1)
    include_extras: bool = False
    """Si False (default): solo commander + mainboard. Si True: también
    companion, sideboard, tokens y maybeboard. En cualquier caso, el estimador
    de coste solo cuenta commander + mainboard."""


class ImportFromTextRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    text: str = Field(..., min_length=1)
    format: str = "commander"
    include_extras: bool = False


class UpdateDeckRequest(BaseModel):
    """Editar metadatos del mazo (nombre, formato, notas)."""
    name: str | None = Field(default=None, min_length=1, max_length=256)
    format: str | None = None
    notes: str | None = None


class AddCardRequest(BaseModel):
    """Añadir una carta a un mazo por nombre (opcional set+num para versión concreta)."""
    name: str = Field(..., min_length=1, max_length=256)
    quantity: int = Field(default=1, ge=1, le=99)
    role: str = "mainboard"
    set_code: str | None = None
    collector_number: str | None = None


class UpdateCardRequest(BaseModel):
    """Editar cantidad o rol de una carta ya en el mazo."""
    quantity: int | None = Field(default=None, ge=1, le=99)
    role: str | None = None


class ChangeArtRequest(BaseModel):
    """Cambia la elección de arte para una carta del mazo.

    Uno de scryfall_id o custom_art_id debe estar poblado.
    Si scryfall_id: se usa oficial de Scryfall y se limpia el custom_art_*.
    Si custom_art_id: se usa el arte custom para la cara indicada.
    """
    deck_card_id: int
    scryfall_id: str | None = None
    custom_art_id: int | None = None
    face: str = "front"  # front | back
    remember_globally: bool = False  # solo aplica a arte oficial


class BuildXMLRequest(BaseModel):
    cardstock: str | None = None
    foil: bool | None = None
    create_run: bool = True
    run_name: str | None = None


class AddCustomArtFromUrlRequest(BaseModel):
    url: str = Field(..., min_length=8)
    card_name: str = Field(..., min_length=1)
    face: str = "front"
    variant: str | None = None


# ---- Responses -----------------------------------------------------------

class ArtOption(BaseModel):
    """Una opción de arte en la galería. Puede ser oficial (Scryfall) o custom local."""
    kind: str = "scryfall"  # "scryfall" | "custom"

    # Scryfall:
    scryfall_id: str | None = None
    set_code: str = ""
    set_name: str = ""
    collector_number: str = ""
    frame: str = ""
    border_color: str = ""
    full_art: bool = False
    textless: bool = False
    promo: bool = False
    layout: str = "normal"
    artist: str | None = None
    released_at: str | None = None
    rarity: str = ""                   # common | uncommon | rare | mythic | special | bonus

    # Custom:
    custom_art_id: int | None = None
    variant_label: str | None = None
    filename: str | None = None

    # Ambos:
    face: str = "front"
    image_small: str | None = None  # URL para thumbnail (scryfall CDN o /custom_art/…)

    # Estado UI:
    is_chosen: bool = False
    is_preferred: bool = False
    is_last_used: bool = False


class RescanResult(BaseModel):
    total: int
    added: int
    removed: int
    kept: int


class DeckCardView(BaseModel):
    id: int
    oracle_id: str
    name: str
    quantity: int
    scryfall_id: str
    custom_art_front_id: int | None = None
    custom_art_back_id: int | None = None
    role: str
    include: bool
    layout: str
    is_dfc: bool
    thumbnail_url: str | None  # URL para thumb (custom si aplica, si no scryfall)
    printings_available: int
    custom_arts_available: int = 0
    history_copies: int = 0
    history_decks: list[str] = []
    # Metadata MTG para ordenar/filtrar en la UI:
    mana_cost: str = ""            # "{2}{U}{U}"
    cmc: float = 0.0               # coste convertido
    type_line: str = ""            # "Legendary Creature — Angel"
    colors: list[str] = []         # ["W","U"]
    color_identity: list[str] = []
    # Reverso para cartas DFC/transform/MDFC:
    back_thumbnail_url: str | None = None
    back_name: str | None = None
    # Cartas relacionadas (tokens, meld_result, meld_part) — para añadir con 1 click:
    related_parts: list[dict[str, str]] = []


class DeckValidation(BaseModel):
    """Validación por formato. Para commander: mainboard+commander = 100."""
    format: str
    expected: int
    counted: int
    is_valid: bool
    message: str
    level: str  # 'ok' | 'warn' | 'error'
    breakdown: dict[str, int] = {}  # role → count


class DeckView(BaseModel):
    id: int
    name: str
    moxfield_id: str | None
    source_url: str | None
    format: str
    commander_scryfall_id: str | None
    imported_at: datetime
    updated_at: datetime
    cards: list[DeckCardView]
    validation: DeckValidation | None = None
