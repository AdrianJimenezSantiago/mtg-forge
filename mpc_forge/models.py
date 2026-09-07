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
    # Cardback específico del mazo. Si != NULL, sustituye al cardback global
    # (`default_cardback_path()`) al generar reversos en modo backs_content='all_cards'.
    # Las cartas DFC / MDFC / meld siguen usando su propio reverso — este cardback
    # SOLO se aplica a los slots que no tienen back_path propio.
    custom_cardback_art_id: Mapped[int | None] = mapped_column(
        ForeignKey("custom_arts.id", ondelete="SET NULL"), nullable=True
    )
    # --- Post-processing config (Fase 3 · T9) ---
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


class DeckActivity(Base):
    """Timeline de eventos ocurridos sobre un mazo.

    Cada operación relevante sobre un mazo (añadir carta, mover entre secciones,
    cambiar arte, generar PDF/XML, localizar, …) inserta una fila aquí. El
    frontend usa esto para pintar el "diario" del mazo en la vista de historial.

    Guardamos snapshots (nombre de la carta, del mazo…) para que el evento siga
    siendo legible aunque después se borre la carta o el mazo. Los detalles
    específicos de cada tipo van en ``payload_json`` como JSON serializado — es
    lo suficientemente flexible como para no tener que migrar el schema cada
    vez que añadimos un nuevo tipo de evento.

    ``kind`` es un string libre en vez de Enum para poder añadir tipos nuevos
    sin migración de BD. Los tipos que reconoce el frontend (con su icono y
    etiqueta) están en el JS de ``history.html``.
    """
    __tablename__ = "deck_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Snapshot del nombre del mazo — persiste si se borra el mazo, para poder
    # mantener eventos "huérfanos" en un futuro "historial global".
    deck_name_snapshot: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    # Tipo de evento. Ver DeckActivityKind en services/deck_activity.py para
    # el listado canónico. Es string libre a propósito (no Enum) para permitir
    # extender sin migración.
    kind: Mapped[str] = mapped_column(String(48), index=True)
    # Snapshot de la carta implicada (si aplica). Muchos eventos no tienen
    # carta asociada (deck_renamed, xml_generated…) — ahí quedan NULL.
    card_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    card_scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_oracle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Payload JSON con detalles específicos del tipo. Nunca vacío — al menos "{}".
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    # Resumen legible pre-computado. El frontend lo usa como fallback si no
    # tiene renderer específico para ``kind``.
    summary: Mapped[str] = mapped_column(String(512), default="")


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
    # Tags extraídos del filename y de la ruta de carpeta (CSV, sin espacios en
    # los tags individuales). Ejemplo: "full_art,retro,anime" para un archivo
    # llamado "Forest (Full Art) (Retro) [Anime].png".
    # Los booleanos derivados están en columnas separadas para permitir queries
    # SQL eficientes con índices simples, sin tener que parsear el CSV en cada
    # búsqueda. Ver `gdrive_indexer.extract_tags()`.
    tags: Mapped[str] = mapped_column(String(512), default="")
    # Flags derivados de tags — indexados para filtrado rápido en el picker.
    is_full_art: Mapped[bool] = mapped_column(Boolean, default=False)
    is_borderless: Mapped[bool] = mapped_column(Boolean, default=False)
    is_extended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_showcase: Mapped[bool] = mapped_column(Boolean, default=False)
    is_retro: Mapped[bool] = mapped_column(Boolean, default=False)
    is_textless: Mapped[bool] = mapped_column(Boolean, default=False)
    is_promo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_alt_art: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Metadatos canónicos (Fase 2 · T5) ---
    # Los usuarios de MPC Autofill convencionalmente etiquetan sus archivos con
    # `[SET NUM]` (ej. "Opt [DMU 100].png") para indicar exactamente qué
    # impresión oficial de Scryfall representa el arte custom. Esto permite
    # vincular sin ambigüedad un arte alternativo a la carta oficial que
    # reproduce, incluso cuando el `filename` es una traducción, artist rename
    # o variante estilística.
    #
    # `expansion_code`: código del set (3-4 chars, minúsculas). Ej. "dmu", "lea".
    # `collector_number`: número dentro del set (string por convención Scryfall:
    #   admite "12★", "4p", "42a" en tokens/promos).
    # `canonical_source`: cómo se detectó el par (filename, folder_path).
    #
    # Ambas columnas son NULL cuando no hay tag `[SET NUM]` — el arte sigue
    # siendo buscable por nombre igual que antes.
    expansion_code: Mapped[str | None] = mapped_column(String(8), default=None, index=True)
    collector_number: Mapped[str | None] = mapped_column(String(16), default=None)
    canonical_source: Mapped[str] = mapped_column(String(16), default="")

    # --- Perceptual hash para dedupe cross-drive (Fase 2 · T8) ---
    # Se calcula opcionalmente al indexar (setting `phash.enabled`, off por
    # default para no gastar bandwidth). El pHash de 64 bits se guarda como
    # 16 chars hexadecimales — barato de comparar con hamming distance.
    # Dos artes con hamming ≤ 8 se consideran "misma imagen" (rango típico
    # para tolerar recompresión/reescalado leve).
    # NULL = aún no calculado. Ver `phash.py`.
    image_hash: Mapped[str | None] = mapped_column(String(16), default=None, index=True)

    # --- URLs directas para tipos no-gdrive (Fase Extras · T7) ---
    # Los tipos de source distintos a Google Drive (HTTPListing, futuros
    # S3/R2, etc.) tienen URLs de descarga y thumbnail arbitrarias que no
    # se pueden derivar del ``file_id``. Antes las codificábamos en el
    # propio file_id con base64 (ver ``HTTPListingSourceType``), lo que
    # limitaba a URLs cortas y complicaba el debug. Estas columnas
    # opcionales guardan las URLs directamente: si están rellenas, los
    # helpers `download_url()` / `thumbnail_url()` del source_type las
    # devuelven tal cual. NULL = usar la derivación heredada (gdrive
    # sigue funcionando como siempre).
    download_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    thumb_url: Mapped[str | None] = mapped_column(String(1024), default=None)

    # --- Tipo de carta: CARD, CARDBACK o TOKEN (Fase 3) ---
    # Determinado exclusivamente por la carpeta contenedora, replicando la
    # lógica de MPC Autofill: si el folder_path contiene un segmento
    # "Cardbacks" → CARDBACK, "Tokens" → TOKEN, resto → CARD.
    # Esto es independiente del tag "back" (que también se asigna a archivos
    # con "(B)" en el nombre, que son caras traseras de DFC, no cardbacks).
    card_type: Mapped[str] = mapped_column(String(16), default="CARD", index=True)


class DFCPair(Base):
    """Par de nombres front → back de una carta doble-cara.

    Se rellena una vez a la semana desde el bulk data de Scryfall (queries
    ``is:dfc`` e ``is:meld``). El sync es idempotente: recrear la tabla no
    duplica filas gracias al UNIQUE en ``front_name``.

    Uso: cuando el usuario importa un mazo por texto plano, si aparece una
    carta cuyo nombre está en ``front_name``, sabemos automáticamente qué
    reverso mostrar sin tener que consultar Scryfall carta a carta. Esto
    acelera imports grandes (100+ cartas) y funciona offline una vez
    sembrado.

    ``kind``:
      - "transform"   : DFC clásicos (Delver of Secrets, etc)
      - "modal_dfc"   : MDFCs de Zendikar Rising en adelante
      - "meld_top"    : la carta se combina con otra para formar un meld_result
                        y su mitad es la de arriba
      - "meld_bottom" : igual pero mitad de abajo
    """
    __tablename__ = "dfc_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    front_name: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    back_name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(24), default="transform")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CollectionEntry(Base):
    """Una carta que el usuario posee, indexada por set + collector_number.

    Independiente de los mazos: tener una carta en un mazo no implica
    poseerla físicamente, y poseerla no implica que esté en ningún mazo.
    El objetivo es trackear colecciones por expansión oficial (checklist
    al estilo "me faltan 12 cartas de Murders at Karlov Manor").

    ``scryfall_id`` es la clave primaria: identifica unívocamente la
    impresión exacta. Los índices en ``set_code`` y ``oracle_id`` aceleran
    las queries "¿cuántas tengo de este set?" y "¿tengo alguna copia de
    esta carta en cualquier set?".
    """
    __tablename__ = "collection_entries"

    scryfall_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    set_code: Mapped[str] = mapped_column(String(16), index=True)
    set_name: Mapped[str] = mapped_column(String(128), default="")
    collector_number: Mapped[str] = mapped_column(String(32), default="")
    rarity: Mapped[str] = mapped_column(String(32), default="common")
    image_small: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class OracleArtistCache(Base):
    """Cache de (oracle_id, artist) para acelerar el recomendador.

    Motivación (Extras · T11): `recommend_by_artist` hace una llamada a
    Scryfall (`prints_by_oracle_id`) por cada oracle_id del mazo. Con 100
    cartas eso son 100 requests y ~50s. Cacheamos las relaciones en local
    para que la segunda vez que se pida el mismo mazo (o parcialmente el
    mismo) responda en <100ms.

    Cada fila es una (oracle_id, artist) — una carta puede tener múltiples
    filas si tiene ediciones de varios artistas. UNIQUE compuesto evita
    duplicados. TTL sugerido: 7 días (Scryfall añade impresiones con cada
    set, ~cada 3 meses).

    Uso:
      - Al llamar al recomendador con un artista X, primero consultamos
        `SELECT oracle_id FROM oracle_artists WHERE artist_folded = ?`
        para saber qué oracle_ids del mazo tienen impresiones de X sin
        tocar Scryfall.
      - Solo caemos a Scryfall para los oracle_ids que faltan del cache
        o cuyas filas están stale.
    """
    __tablename__ = "oracle_artists"
    __table_args__ = (
        UniqueConstraint("oracle_id", "artist_folded",
                         name="uq_oracle_artists_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oracle_id: Mapped[str] = mapped_column(String(64), index=True)
    # ``artist_folded`` es el artist con asciifolding + lowercase (misma
    # normalización que ``recommender._fold``). Indexado para lookup rápido.
    artist_folded: Mapped[str] = mapped_column(String(128), index=True)
    # Nombre display del artist (con casing/acentos originales) — para UI.
    artist_display: Mapped[str] = mapped_column(String(128), default="")
    # Cuándo se pobló esta fila. Usado para invalidar por TTL.
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

