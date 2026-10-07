from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ImportFromMoxfieldRequest(BaseModel):
    url_or_id: str = Field(..., min_length=1)
    include_extras: bool = False


class ImportFromUrlRequest(BaseModel):
    url: str = Field(..., min_length=8)
    name: str | None = Field(default=None, max_length=256)
    format: str = "commander"
    include_extras: bool = False


class SupportedSite(BaseModel):
    key: str
    name: str
    example_url: str
    host_names: list[str]


class ImportFromTextRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    text: str = Field(..., min_length=1)
    format: str = "commander"
    include_extras: bool = False


class UpdateDeckRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    format: str | None = None
    notes: str | None = None


class AddCardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    quantity: int = Field(default=1, ge=1, le=99)
    role: str = "mainboard"
    set_code: str | None = None
    collector_number: str | None = None


class UpdateCardRequest(BaseModel):
    quantity: int | None = Field(default=None, ge=1, le=99)
    role: str | None = None


class ChangeArtRequest(BaseModel):
    deck_card_id: int
    scryfall_id: str | None = None
    custom_art_id: int | None = None
    face: str = "front"
    remember_globally: bool = False


class BuildXMLRequest(BaseModel):
    cardstock: str | None = None
    foil: bool | None = None
    create_run: bool = True
    run_name: str | None = None
    web_mode: bool = False


class AddCustomArtFromUrlRequest(BaseModel):
    url: str = Field(..., min_length=8)
    card_name: str = Field(..., min_length=1)
    face: str = "front"
    variant: str | None = None


class ArtOption(BaseModel):
    kind: str = "scryfall"

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
    rarity: str = ""

    custom_art_id: int | None = None
    variant_label: str | None = None
    filename: str | None = None

    face: str = "front"
    image_small: str | None = None
    thumb_url: str | None = None

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
    thumbnail_url: str | None
    printings_available: int
    custom_arts_available: int = 0
    history_copies: int = 0
    history_decks: list[str] = []
    mana_cost: str = ""
    cmc: float = 0.0
    type_line: str = ""
    colors: list[str] = []
    color_identity: list[str] = []
    rarity: str = ""
    keywords: list[str] = []
    back_thumbnail_url: str | None = None
    back_name: str | None = None
    related_parts: list[dict[str, str]] = []


class IllegalCardView(BaseModel):
    name: str
    status: str
    role: str


class DeckValidation(BaseModel):
    format: str
    expected: int
    counted: int
    is_valid: bool
    message: str
    level: str
    breakdown: dict[str, int] = {}
    illegal: list[IllegalCardView] = []


class DeckPriceView(BaseModel):
    eur: float = 0.0
    usd: float = 0.0
    priced_cards: int = 0
    unpriced_cards: int = 0


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
    price: DeckPriceView | None = None
    cover_card_id: int | None = None


class UnresolvedEntry(BaseModel):
    name: str
    quantity: int = 1
    raw_line: str | None = None
    set: str | None = None
    number: str | None = None
    role: str = "mainboard"
    reason: str = "not_found_on_scryfall"


class ImportResult(BaseModel):
    deck: DeckView
    unresolved: list[UnresolvedEntry] = []
    resolved_count: int = 0
    total_entries: int = 0


class ArtOptionsPage(BaseModel):
    items: list[ArtOption] = Field(default_factory=list)
    custom: list[ArtOption] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 60
    has_more: bool = False
    facets: dict[str, int] = Field(default_factory=dict)
