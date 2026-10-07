from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TZDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class PrintingCache(Base):
    __tablename__ = "printings"

    scryfall_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    oracle_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256), index=True)
    set_code: Mapped[str] = mapped_column(String(16))
    set_name: Mapped[str] = mapped_column(String(128))
    collector_number: Mapped[str] = mapped_column(String(32))
    rarity: Mapped[str] = mapped_column(String(32))
    lang: Mapped[str] = mapped_column(String(8), default="en")
    frame: Mapped[str] = mapped_column(String(16), default="")
    border_color: Mapped[str] = mapped_column(String(16), default="")
    full_art: Mapped[bool] = mapped_column(Boolean, default=False)
    textless: Mapped[bool] = mapped_column(Boolean, default=False)
    promo: Mapped[bool] = mapped_column(Boolean, default=False)
    layout: Mapped[str] = mapped_column(String(32), default="normal")
    mana_cost: Mapped[str] = mapped_column(String(64), default="")
    cmc: Mapped[float] = mapped_column(default=0.0)
    type_line: Mapped[str] = mapped_column(String(128), default="")
    colors: Mapped[str] = mapped_column(String(16), default="")
    color_identity: Mapped[str] = mapped_column(String(16), default="")
    keywords: Mapped[str] = mapped_column(String(512), default="")
    image_normal: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_large: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_png: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_image_normal: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_image_large: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_image_png: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(128), nullable=True)
    released_at: Mapped[str | None] = mapped_column(String(16), nullable=True)
    finishes: Mapped[str] = mapped_column(String(64), default="nonfoil")
    price_usd: Mapped[float | None] = mapped_column(nullable=True)
    price_usd_foil: Mapped[float | None] = mapped_column(nullable=True)
    price_eur: Mapped[float | None] = mapped_column(nullable=True)
    legalities: Mapped[str] = mapped_column(Text, default="")
    related_parts: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)


class LocalArt(Base):
    __tablename__ = "local_arts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    scryfall_id: Mapped[str] = mapped_column(String(64), index=True)
    face: Mapped[str] = mapped_column(String(16), default="front")
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    thumb_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("scryfall_id", "face", name="uq_local_art_scryfall_face"),)


class ArtPreference(Base):
    __tablename__ = "art_preferences"

    oracle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow, onupdate=_utcnow)


class CustomArt(Base):
    __tablename__ = "custom_arts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512))
    relative_path: Mapped[str] = mapped_column(Text)
    card_name_normalized: Mapped[str] = mapped_column(String(256), index=True)
    variant_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    face: Mapped[str] = mapped_column(String(16), default="front")
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)


class Deck(Base):
    __tablename__ = "decks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    moxfield_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str] = mapped_column(String(32), default="commander")
    commander_scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_cardback_art_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow, onupdate=_utcnow)

    cards: Mapped[list[DeckCard]] = relationship(
        back_populates="deck", cascade="all, delete-orphan"
    )


class DeckCard(Base):
    __tablename__ = "deck_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("decks.id", ondelete="CASCADE"))
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    scryfall_id: Mapped[str] = mapped_column(String(64))
    custom_art_front_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    custom_art_back_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), default="mainboard")
    include: Mapped[bool] = mapped_column(Boolean, default=True)

    deck: Mapped[Deck] = relationship(back_populates="cards")


class PrintRun(Base):
    __tablename__ = "print_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    cardstock: Mapped[str] = mapped_column(String(64), default="(S30) Standard Smooth")
    foil: Mapped[bool] = mapped_column(Boolean, default=False)
    total_cards: Mapped[int] = mapped_column(Integer, default=0)
    tier_size: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_eur: Mapped[float] = mapped_column(default=0.0)
    xml_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[PrintRunItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PrintRunItem(Base):
    __tablename__ = "print_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("print_runs.id", ondelete="CASCADE"))
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    deck_name: Mapped[str] = mapped_column(String(256))
    scryfall_id: Mapped[str] = mapped_column(String(64), index=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    card_name: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    run: Mapped[PrintRun] = relationship(back_populates="items")


class PhysicalInventory(Base):
    __tablename__ = "physical_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="ready")
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow, onupdate=_utcnow)


class DeckActivity(Base):
    __tablename__ = "deck_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    deck_name_snapshot: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    card_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    card_scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_oracle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(String(512), default="")


class KeyValue(Base):
    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ArtSource(Base):
    __tablename__ = "art_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(32), default="gdrive")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(256), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    indexed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    indexed_files: Mapped[int] = mapped_column(Integer, default=0)
    index_error: Mapped[str] = mapped_column(Text, default="")


class IndexedArt(Base):
    __tablename__ = "indexed_art"
    __table_args__ = (UniqueConstraint("source_id", "file_id", name="uq_indexed_source_file"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("art_sources.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[str] = mapped_column(String(128), index=True)
    filename: Mapped[str] = mapped_column(String(512), index=True)
    name_normalized: Mapped[str] = mapped_column(String(512), index=True)
    folder_path: Mapped[str] = mapped_column(String(1024), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(64), default="")
    indexed_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    tags: Mapped[str] = mapped_column(String(512), default="")
    is_full_art: Mapped[bool] = mapped_column(Boolean, default=False)
    is_borderless: Mapped[bool] = mapped_column(Boolean, default=False)
    is_extended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_showcase: Mapped[bool] = mapped_column(Boolean, default=False)
    is_retro: Mapped[bool] = mapped_column(Boolean, default=False)
    is_textless: Mapped[bool] = mapped_column(Boolean, default=False)
    is_promo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_alt_art: Mapped[bool] = mapped_column(Boolean, default=False)

    expansion_code: Mapped[str | None] = mapped_column(String(8), default=None, index=True)
    collector_number: Mapped[str | None] = mapped_column(String(16), default=None)
    canonical_source: Mapped[str] = mapped_column(String(16), default="")

    image_hash: Mapped[str | None] = mapped_column(String(16), default=None, index=True)

    download_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    thumb_url: Mapped[str | None] = mapped_column(String(1024), default=None)

    card_type: Mapped[str] = mapped_column(String(16), default="CARD", index=True)


class DFCPair(Base):
    __tablename__ = "dfc_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    front_name: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    back_name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(24), default="transform")
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)


class CollectionEntry(Base):
    __tablename__ = "collection_entries"

    scryfall_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    set_code: Mapped[str] = mapped_column(String(16), index=True)
    set_name: Mapped[str] = mapped_column(String(128), default="")
    collector_number: Mapped[str] = mapped_column(String(32), default="")
    rarity: Mapped[str] = mapped_column(String(32), default="common")
    image_small: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)


class OracleArtistCache(Base):
    __tablename__ = "oracle_artists"
    __table_args__ = (
        UniqueConstraint("oracle_id", "artist_folded", name="uq_oracle_artists_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    artist_folded: Mapped[str] = mapped_column(String(128), index=True)
    artist_display: Mapped[str] = mapped_column(String(128), default="")
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)


class DeckSnapshot(Base):
    __tablename__ = "deck_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow, index=True)
    card_count: Mapped[int] = mapped_column(Integer, default=0)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class ArtTheme(Base):
    __tablename__ = "art_themes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)

    entries: Mapped[list[ArtThemeEntry]] = relationship(
        back_populates="theme", cascade="all, delete-orphan"
    )


class ArtThemeEntry(Base):
    __tablename__ = "art_theme_entries"
    __table_args__ = (UniqueConstraint("theme_id", "oracle_id", name="ux_art_theme_entry"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("art_themes.id", ondelete="CASCADE"), index=True
    )
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    card_name: Mapped[str] = mapped_column(String(256), default="")
    scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_art_front_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_art_back_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    theme: Mapped[ArtTheme] = relationship(back_populates="entries")


class BulkSyncState(Base):
    __tablename__ = "bulk_sync_state"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    updated_at: Mapped[str] = mapped_column(String(64), default="")
    synced_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
