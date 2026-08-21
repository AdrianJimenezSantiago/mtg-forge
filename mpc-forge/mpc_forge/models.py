"""SQLAlchemy ORM models.

Diseño:
- `PrintingCache`: catálogo de impresiones (por scryfall_id). Se rellena bajo demanda.
- `LocalArt`: archivo físico de arte descargado (con hash para dedupe absoluto).
- `ArtPreference`: elección persistente del usuario POR oracle_id.
- `Deck`: mazo importado.
- `DeckCard`: cartas dentro del mazo con la impresión elegida.
- `PrintRun`: cada vez que el usuario "envía a MPC" un mazo (o varios).
- `PrintRunItem`: qué cartas y cuántas copias entraron en cada run.
- `PhysicalInventory`: opcional. Estado físico por copia impresa.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Timestamp aware en UTC. Usamos default Python en lugar de server_default
    para evitar lazy-loads sincrónicos post-commit con aiosqlite."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PrintingCache(Base):
    """Una impresión concreta de una carta en Scryfall.

    Cacheamos los campos que necesitamos para pintar la galería sin volver a llamar a Scryfall.
    """
    __tablename__ = "printings"

    scryfall_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
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
    # Metadata para ordenar/filtrar sin volver a llamar a Scryfall:
    mana_cost: Mapped[str] = mapped_column(String(64), default="")           # ej. "{2}{U}{U}"
    cmc: Mapped[float] = mapped_column(default=0.0)                          # coste convertido
    type_line: Mapped[str] = mapped_column(String(128), default="")          # "Legendary Creature — Elf"
    colors: Mapped[str] = mapped_column(String(16), default="")              # csv "W,U,B"
    color_identity: Mapped[str] = mapped_column(String(16), default="")      # csv "W,U,B"
    keywords: Mapped[str] = mapped_column(String(512), default="")           # csv
    image_normal: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_large: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_png: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Para DFC guardamos también los datos de la cara trasera:
    back_image_normal: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_image_large: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_image_png: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(128), nullable=True)
    released_at: Mapped[str | None] = mapped_column(String(16), nullable=True)
    finishes: Mapped[str] = mapped_column(String(64), default="nonfoil")  # csv
    # Partes relacionadas: JSON compacto con [{"id":"...", "name":"...", "component":"token|meld_result|meld_part"}, ...]
    # Antes: solo tokens. Ahora: también meld results/parts para automatizar la adición al mazo.
    related_parts: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LocalArt(Base):
    """Un archivo de arte descargado a disco.

    `sha256` es la clave real de dedupe: si dos scryfall_ids devuelven bytes idénticos
    se apuntan al mismo LocalArt (poco común, pero cubre casos de reimpresiones idénticas).
    """
    __tablename__ = "local_arts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    scryfall_id: Mapped[str] = mapped_column(String(64), index=True)
    face: Mapped[str] = mapped_column(String(16), default="front")  # front|back
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("scryfall_id", "face", name="uq_local_art_scryfall_face"),
    )


class ArtPreference(Base):
    """Preferencia del usuario para representar una carta (por oracle_id)."""
    __tablename__ = "art_preferences"

    oracle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class CustomArt(Base):
    """Un archivo de arte custom (o alternativo) que el usuario dropea en la
    carpeta `custom_art/`. Se indexa por el nombre de carta normalizado, y aparece
    en la galería junto a las impresiones oficiales de Scryfall.

    Convenciones de nombrado:
        Sol Ring.png                    → Sol Ring, front
        Sol Ring - Anime.png            → Sol Ring, front, variant="Anime"
        Sol Ring (Retro Frame).png      → Sol Ring, front, variant="Retro Frame"
        Delver of Secrets [BACK].png    → Delver of Secrets, back
    """
    __tablename__ = "custom_arts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512))  # nombre original mostrable
    relative_path: Mapped[str] = mapped_column(Text)     # bajo PATHS.custom_art_dir
    card_name_normalized: Mapped[str] = mapped_column(String(256), index=True)
    variant_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    face: Mapped[str] = mapped_column(String(16), default="front")  # front|back
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Deck(Base):
    __tablename__ = "decks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    moxfield_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str] = mapped_column(String(32), default="commander")
    commander_scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    cards: Mapped[list["DeckCard"]] = relationship(
        back_populates="deck", cascade="all, delete-orphan"
    )


class DeckCard(Base):
    """Una entrada del decklist con la impresión que se usará al imprimir."""
    __tablename__ = "deck_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("decks.id", ondelete="CASCADE"))
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # Impresión "oficial" elegida en Scryfall — se usa como fallback y para meta.
    scryfall_id: Mapped[str] = mapped_column(String(64))
    # Si != NULL, se usa este arte custom local en lugar del oficial.
    custom_art_front_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    # Solo aplica a DFC: reverso custom.
    custom_art_back_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    # Rol dentro del mazo: commander, mainboard, companion, sideboard, tokens
    role: Mapped[str] = mapped_column(String(32), default="mainboard")
    include: Mapped[bool] = mapped_column(Boolean, default=True)

    deck: Mapped["Deck"] = relationship(back_populates="cards")


class PrintRun(Base):
    __tablename__ = "print_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    cardstock: Mapped[str] = mapped_column(String(64), default="(S30) Standard Smooth")
    foil: Mapped[bool] = mapped_column(Boolean, default=False)
    total_cards: Mapped[int] = mapped_column(Integer, default=0)
    tier_size: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_eur: Mapped[float] = mapped_column(default=0.0)
    xml_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["PrintRunItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PrintRunItem(Base):
    __tablename__ = "print_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("print_runs.id", ondelete="CASCADE"))
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    deck_name: Mapped[str] = mapped_column(String(256))  # snapshot por si borran el mazo
    scryfall_id: Mapped[str] = mapped_column(String(64), index=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    card_name: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    run: Mapped["PrintRun"] = relationship(back_populates="items")


class PhysicalInventory(Base):
    """Estado físico opcional. Independiente del historial de impresión."""
    __tablename__ = "physical_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="ready")  # ready|cut|sleeved|lost
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class KeyValue(Base):
    """Pequeño store clave-valor para settings serializados y flags."""
    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ArtSource(Base):
    """Google Drive u otra fuente comunitaria de arte custom.

    Se gestionan a mano desde la UI de Ajustes. Cada source es un link que el
    usuario puede abrir en el navegador para explorar y descargar imágenes,
    o marcar como su preferida para consultas rápidas desde el editor.
    """
    __tablename__ = "art_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))          # ej. "Cardstock Con"
    url: Mapped[str] = mapped_column(String(512))           # URL completa de la carpeta de Drive
    source_type: Mapped[str] = mapped_column(String(32), default="gdrive")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(256), default="")  # csv (ej. "commander,proxy")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # aparece destacado en editor
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Estado de indexación (fuzzy search interno):
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    indexed_files: Mapped[int] = mapped_column(Integer, default=0)
    index_error: Mapped[str] = mapped_column(Text, default="")


class IndexedArt(Base):
    """Cada archivo de imagen descubierto al indexar un ArtSource.

    Solo guardamos los metadatos suficientes para buscar y obtener la URL de
    descarga/thumbnail. Nunca descargamos la imagen hasta que el usuario elige
    usarla explícitamente en el editor.
    """
    __tablename__ = "indexed_art"
    __table_args__ = (UniqueConstraint("source_id", "file_id", name="uq_indexed_source_file"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("art_sources.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[str] = mapped_column(String(128), index=True)  # google drive file id
    filename: Mapped[str] = mapped_column(String(512), index=True)
    # Nombre normalizado para búsqueda (lowercase, sin extensión, sin puntuación):
    name_normalized: Mapped[str] = mapped_column(String(512), index=True)
    folder_path: Mapped[str] = mapped_column(String(1024), default="")  # subruta dentro del drive
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(64), default="")
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

