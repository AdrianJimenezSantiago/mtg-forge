"""Indexado de Google Drives para búsqueda de arte custom.

Dos modos:
1. **API v3** (recomendado): con GOOGLE_API_KEY configurada. Rápido, fiable,
   incluye tamaño y mime type. Cuota: 10.000 requests/día gratis.
2. **Scraping HTML** (fallback): sin API key. Parsea el HTML del
   `embeddedfolderview` que Google renderiza para carpetas públicas.
   Funciona pero es más lento, no da tamaño y algunas carpetas grandes se
   quedan cortas.

El indexado NO descarga imágenes — solo lee metadatos. La descarga solo ocurre
cuando el usuario elige "Usar este arte" en el editor de mazo, y entonces se
guarda en `custom_art/_downloaded/` como cualquier otra imagen por URL.

**Concurrencia**: SQLite serializa escrituras. Cuando el usuario pide
"Indexar todos", si lanzáramos 67 tareas en paralelo se pelearían por el lock
y muchas fallarían con "database is locked". Usamos un semáforo global para
que solo se indexe un drive a la vez, aunque el usuario lance muchos.
Se ejecutan en background secuencialmente.

Se lanza bajo demanda desde la UI (botón "Indexar" en cada drive). No hay cron
automático — el usuario decide cuándo re-indexar (ej. cuando ve que le faltan
artes nuevos).
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

# Versión del normalizador. Cuando cambiamos la lógica de `normalize_filename`,
# incrementamos este valor y el startup ejecuta un backfill idempotente que
# recalcula `IndexedArt.name_normalized` sobre todo el índice existente sin
# perder los file_ids indexados. Ver `backfill_normalized_names()`.
#
# Cambios por versión:
#   1: normalización base (lowercase, sin paréntesis, split por variante)
#   2: añadido asciifolding para manejar acentos y diacríticos.
#   3: añadida extracción de tags (`is_full_art`, `is_borderless`, …). El
#      backfill escribe tags sobre filas ya indexadas sin re-descargar.
#   4: añadida extracción de metadatos canónicos [SET NUM]. El backfill
#      rellena expansion_code / collector_number / canonical_source.
#   5: extract_tags también matchea segmentos de folder sin brackets
#      (Extras · F1/T4) y respeta overrides del vocabulario del usuario.
#   6: añadido card_type (CARD, CARDBACK, TOKEN) basado en carpeta.
#      Replica la lógica de MPC Autofill para distinguir cardbacks reales
#      (en carpeta Cardbacks/) de caras traseras de DFC con (B) en el nombre.
NORMALIZATION_VERSION = 6


# Semáforo global: máximo 3 indexados concurrentes.
# Con WAL mode + busy_timeout=30s, SQLite aguanta bien 3 escritores en paralelo.
# El bottleneck real es Google Drive API (rate limit ~10 req/s por usuario), no SQLite.
# Un drive gigante (source 3 tiene decenas de miles de imágenes) puede tardar minutos;
# con concurrencia=3 los otros drives no esperan innecesariamente.
_INDEX_SEMAPHORE = asyncio.Semaphore(3)

# Commits parciales cada N filas — evitamos mantener un lock de escritura
# demasiado tiempo con drives gigantes, y damos progreso visible en la UI.
# Compartido entre `_index_via_api` (gdrive) y `_index_generic` (resto).
_COMMIT_EVERY = 500


# ---------------------------------------------------------------------------
# Nombres normalizados para fuzzy search
# ---------------------------------------------------------------------------
# Filosofía: queremos que "Forest (Full Art).png", "Forest - Alt by Chowning.png",
# y "Forest.png" TODOS se normalicen a "forest" (nombre canónico de la carta).
# En cambio "Forest Warden.png" se queda como "forest warden" — es una carta
# distinta. Así el matching exacto ya nos filtra el ruido.

_STRIP_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*[\[\(\{].*?[\]\)\}]\s*")   # elimina "(Anime)" "[BACK]" etc.
# Corta el nombre en el primer separador de variante ("-", "by", "feat", "|"):
_VARIANT_SPLIT_RE = re.compile(
    r"\s+(?:-|—|–|by|feat(?:\.|uring)?|\||//)\s+", re.IGNORECASE
)
_NONALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_MULTISPACE_RE = re.compile(r"\s+")


def _asciifold(text: str) -> str:
    """Reduce caracteres Unicode con diacríticos a su equivalente ASCII.

    Ejemplos:
      "Jayā Ballard"  → "Jaya Ballard"
      "Café"          → "Cafe"
      "naïve"         → "naive"
      "Æther Vial"    → "aether Vial"   (ligadura común en MTG)

    Estrategia: NFKD descompone caracteres en base + combining marks
    (ej. "á" → "a" + U+0301 COMBINING ACUTE ACCENT). Filtramos por
    ``unicodedata.combining()`` para descartar solo los marks, dejando
    intactos números, símbolos monetarios, etc.

    Luego traducimos manualmente ligaduras que NFKD no descompone
    (Æ, æ, Œ, œ, ß) para cubrir cartas como Æther / Aether que aparecen
    en ambas grafías según la impresión.
    """
    if not text:
        return text
    # Ligaduras que NFKD deja intactas — las mapeamos a su forma expandida.
    text = (
        text.replace("Æ", "AE").replace("æ", "ae")
        .replace("Œ", "OE").replace("œ", "oe")
        .replace("ß", "ss")
    )
    # NFKD descompone. Filtramos marks combinantes (Mn).
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_filename(name: str) -> str:
    """Convierte cualquier variante de filename al nombre canónico de la carta.

    Ejemplos:
      "Forest.png"                          → "forest"
      "Forest (Full Art).png"               → "forest"
      "Forest - Alt Art.png"                → "forest"
      "Forest by Chowning.png"              → "forest"
      "Forest [BACK].png"                   → "forest"
      "Sol Ring (Daubrez Borderless).png"   → "sol ring"
      "Bruna, the Fading Light (Women's Day).jpg" → "bruna the fading light"
      "Forest Warden.png"                   → "forest warden"  (carta distinta)

    Asciifolding (NORMALIZATION_VERSION >= 2):
      "Jayā Ballard.png"                    → "jaya ballard"
      "Æther Vial.png"                      → "aether vial"
      "Naïve Believer.png"                  → "naive believer"

    La misma pipeline se aplica al query del usuario en `gdrive_search.search()`,
    de forma que "jaya" matchea a "Jayā" y viceversa.
    """
    if not name:
        return ""
    n = _STRIP_EXT_RE.sub("", name)
    n = _PAREN_RE.sub(" ", n)      # quita paréntesis, corchetes, llaves
    # Cortar por " - ", " by ", " | ", etc. — nos quedamos solo con la parte previa
    parts = _VARIANT_SPLIT_RE.split(n, maxsplit=1)
    n = parts[0]
    n = _asciifold(n)              # acentos y ligaduras → ASCII
    n = n.lower()
    n = _NONALNUM_RE.sub(" ", n)   # cualquier no-alfanumérico → espacio
    n = _MULTISPACE_RE.sub(" ", n).strip()
    return n


# ---------------------------------------------------------------------------
# Extracción de tags
# ---------------------------------------------------------------------------
# Filosofía: MPC Autofill descarta los `()` y `[]` al normalizar el nombre pero
# los preserva como tags filtrables. Adoptamos el mismo enfoque: extraemos el
# contenido de todos los paréntesis/corchetes del filename y del folder_path,
# los matcheamos contra un vocabulario canónico con aliases, y devolvemos un
# CSV listo para guardar en `IndexedArt.tags`.
#
# El vocabulario está pensado para MTG proxy art:
#   - "Full art"   → full_art
#   - "Borderless" → borderless
#   - "Retro"      → retro
#   - "Textless"   → textless
#   - etc.
#
# Los flags booleanos derivados (is_full_art, is_borderless…) se calculan aquí
# también, para que el indexer los pueda escribir sin lógica duplicada.
#
# Extras · F1/T4: el vocabulario es EDITABLE por el usuario. Al arrancar,
# `_load_user_vocab_overrides()` mira si existe
# ``<data_dir>/tag_vocabulary.json`` y lo mergea sobre el default. Formato:
#
#   {"aliases": {"my_custom_tag": ["gold border", "silver bordered"], ...}}
#
# Los tags definidos por el usuario se emiten como tags en el CSV pero NO
# generan columnas booleanas nuevas (las columnas son fijas). Los usuarios
# pueden filtrar por ellos vía FTS5 sobre el campo `tags`.

# Vocabulario canónico DEFAULT: canonical_tag → set de aliases lowercase.
# Los aliases se comparan contra el contenido bruto de los brackets, permitiendo
# múltiples formas de nombrar el mismo concepto ("FA" = "full art").
#
# Este diccionario replica el "official tag vocabulary" documentado por MPCFill
# — la herramienta de la que provienen la mayoría de drives indexables. La
# fuente son las guidelines que MPCFill publica sobre nombrado de archivos.
# Referencia: https://mpcfill.com/ (sección de tags).
#
# ORGANIZACIÓN — canonical_tag → aliases:
#   1. FRAME BÁSICOS: los 8 que además tienen columna en IndexedArt
#      (full_art, borderless, extended, showcase, retro, textless, promo,
#      alt_art). Estos son los únicos con is_* columns, por eso reciben
#      filtrado SQL directo. Todos los demás viven en el CSV `tags`.
#   2. FRAME MPCFILL: variantes de showcase por set (kaladesh_inventions,
#      dominaria_stained_glass, phyrexia_oil, …), frames especiales
#      (m15, modern_frame, futureshifted, planeshifted, …).
#   3. ART: variantes de arte (altered, pixel_art, sketch_art, ai_art,
#      artist_art, upscaled_scan).
#   4. MISC: nickname, realistic, secret_lair, non_black_border variants.
#   5. NSFW.
#   6. UNIVERSE: temáticas cross-property (fallout, final_fantasy,
#      warhammer_40k, lord_of_the_rings, dr_who, …).
#   7. IDIOMA + META: japanese, foil, back.
_DEFAULT_TAG_VOCABULARY: dict[str, frozenset[str]] = {

    # ─── (1) Frames básicos con columna is_* propia ─────────────────────────
    "full_art": frozenset({
        "full art", "fullart", "full-art", "fa",
        "full-art frame", "full art frame", "fullart frame",
    }),
    "borderless": frozenset({
        "borderless", "no border", "no-border", "bl",
        "borderless art", "borderless frame",
    }),
    "extended": frozenset({
        "extended", "extended art", "extended-art", "ea",
        "extended frame", "extended art frame",
    }),
    "showcase": frozenset({
        "showcase", "sc",
        "showcase frame", "extension showcase frame", "extension frame",
    }),
    "retro": frozenset({
        "retro", "retro frame", "old border", "old frame",
        "1993 frame", "old-frame",
        "ancient frame", "ancient",
        "original frame", "og frame",
        "alpha", "beta", "unlimited", "abu", "classic",
    }),
    "textless": frozenset({
        "textless", "no text", "no-text", "textless card",
    }),
    "promo": frozenset({
        "promo", "pre-release", "prerelease", "pre release", "release",
    }),
    "alt_art": frozenset({
        "alt art", "alt-art", "alternate art", "alternate", "alt",
        "alternative art", "custom art",
    }),

    # ─── (2a) Frames MPCFill "top-level" (no showcase) ──────────────────────
    "post_2023_borderless": frozenset({
        "post-2023 borderless", "borderless 2023", "borderless alt",
    }),
    "custom_frame": frozenset({
        "custom-made frame", "custom frame",
    }),
    "ai_frame": frozenset({
        "ai frame",
    }),
    "kaladesh_dark": frozenset({
        "kaladesh dark", "kaladesh dark frame",
    }),
    "minimalist": frozenset({
        "minimalist", "minimalist frame", "min",
    }),
    "simple_inventions": frozenset({
        "simple inventions", "simple inventions frame",
    }),
    "stonecutter": frozenset({
        "stonecutter", "stonecutter frame",
    }),
    "fnm_promo": frozenset({
        "fnm promo", "fnm promo frame", "fnm frame",
        "universal promo frame", "universal promo",
        "wpn promo frame", "wpn promo",
    }),
    "foil_etched": frozenset({
        "foil-etched", "foil-etched frame", "etched frame", "etched",
    }),
    "full_text": frozenset({
        "full text", "full text frame",
    }),
    "futureshifted": frozenset({
        "futureshifted", "futureshifted frame",
        "fut frame", "future sight frame", "future shifted frame", "future shifted",
    }),
    "m15": frozenset({
        "m15", "m15 frame", "regular frame",
    }),
    "modern_frame": frozenset({
        "modern", "modern frame",
        "eighth edition frame", "8th edition frame",
        "eighth edition", "8th edition", "8ed",
    }),
    "planeshifted": frozenset({
        "planeshifted", "planeshifted frame",
        "colorshifted frame", "planar chaos frame", "plc frame",
    }),
    "universes_beyond": frozenset({
        "universes beyond", "ub frame", "universes beyond frame", "ub",
    }),

    # ─── (2b) Showcase MPCFill — variantes por set/tema ──────────────────────
    # Todas se meten en el CSV `tags`. Búsqueda FTS5 las encuentra por texto.
    "amonkhet_invocations": frozenset({
        "amonkhet invocations", "akh invocations",
    }),
    "assassins_creed_memory_corridor": frozenset({
        "assassins creed memory corridor",
        "memory corridor frame", "memory corridor", "acn frame",
    }),
    "avatar_elemental": frozenset({
        "avatar elemental", "avatar elemental frame", "elemental frame",
    }),
    "bloomburrow_borderless": frozenset({
        "bloomburrow borderless", "bloomburrow borderless frame",
    }),
    "bloomburrow_woodland": frozenset({
        "bloomburrow woodland", "woodland frame", "woodland",
    }),
    "capenna_art_deco": frozenset({
        "capenna art deco", "snc art deco frame", "capenna art deco frame",
        "new capenna art deco", "new capenna art deco frame",
    }),
    "capenna_golden_age": frozenset({
        "capenna golden age", "snc golden age frame", "capenna golden age frame",
        "new capenna golden age", "new capenna golden age frame",
    }),
    "capenna_skyscraper": frozenset({
        "capenna skyscraper", "snc skyscraper frame", "capenna skyscraper frame",
        "new capenna skyscraper", "new capenna skyscraper frame",
    }),
    "classicshifted": frozenset({
        "classicshifted", "classicshifted frame",
    }),
    "commander_legends": frozenset({
        "commander legends", "commander legends frame", "cmr frame",
    }),
    "dnd_module": frozenset({
        "d&d module", "d&d module frame",
    }),
    "dnd_sourcebook": frozenset({
        "d&d sourcebook", "d&d sourcebook frame",
    }),
    "doctor_who_tardis": frozenset({
        "doctor who tardis", "doctor who tardis frame",
        "who frame", "doctor who", "tardis", "tardis frame",
    }),
    "dominaria_stained_glass": frozenset({
        "dominaria stained glass", "dmu frame",
        "stained glass frame", "dominaria stained glass frame", "stained glass",
    }),
    "dragonstorm_ghostfire": frozenset({
        "dragonstorm ghostfire", "ghostfire frame", "ghostfire",
    }),
    "duskmourn_paranormal": frozenset({
        "duskmourn paranormal", "paranormal frame", "paranormal", "dsk frame",
    }),
    "eclipsed_fable": frozenset({
        "eclipsed fable", "fable frame", "fable", "ecl frame",
    }),
    "edge_of_eternities_stellar_sights": frozenset({
        "edge of eternities stellar sights",
        "stellar sights frame", "stellar sights", "eos frame",
    }),
    "eldraine_enchanting_tales": frozenset({
        "eldraine enchanting tales",
        "wot frame", "enchanting tales frame", "enchanting tales",
        "eldraine enchanting tales frame",
    }),
    "eldraine_storybook": frozenset({
        "eldraine storybook",
        "eld frame", "woe frame", "eldraine frame", "wilds of eldraine frame",
        "storybook frame", "eldraine storybook frame",
    }),
    "english_mystical_archive": frozenset({
        "english mystical archive", "en sta frame",
    }),
    "fca_showcase": frozenset({
        "fca showcase", "fca showcase frame", "fca frame",
        "final fantasy frame",
        "borderless source material", "source material",
    }),
    "ikoria_crystal": frozenset({
        "ikoria crystal", "ikoria crystal frame", "crystal frame",
    }),
    "innistrad_equinox": frozenset({
        "innistrad equinox", "mid frame",
        "innistrad equinox frame", "equinox frame", "midnight hunt frame",
    }),
    "innistrad_fang": frozenset({
        "innistrad fang", "vow frame",
        "fang frame", "crimson vow frame", "innistrad fang frame",
    }),
    "ixalan_coin": frozenset({
        "ixalan coin", "ixalan coin frame", "coin frame",
    }),
    "japanese_mystical_archive": frozenset({
        "japanese mystical archive", "jp sta frame",
    }),
    "japan_showcase": frozenset({
        "japan showcase", "japan showcase frame",
        "jp showcase", "jp showcase frame",
    }),
    "kaladesh_inventions": frozenset({
        "kaladesh inventions", "kld inventions",
    }),
    "kaldheim_viking": frozenset({
        "kaldheim viking", "khm frame",
        "viking frame", "kaldheim frame", "kaldheim viking frame",
    }),
    "kamigawa_neon": frozenset({
        "kamigawa neon", "neo neon frame",
        "kamigawa neon frame", "neon dynasty neon frame", "neon frame",
    }),
    "kamigawa_ninja": frozenset({
        "kamigawa ninja", "neo ninja frame",
        "kamigawa ninja frame", "neon dynasty ninja frame", "ninja frame",
    }),
    "kamigawa_samurai": frozenset({
        "kamigawa samurai", "neo samurai frame",
        "kamigawa samurai frame", "neon dynasty samurai frame", "samurai frame",
    }),
    "karlov_dossier": frozenset({
        "karlov dossier", "dossier frame", "dossier", "mkm frame",
    }),
    "lotr_ring": frozenset({
        "lotr ring", "ltr frame", "lotr ring frame", "ring frame",
    }),
    "lotr_scrolls_of_middle_earth": frozenset({
        "lotr scrolls of middle-earth", "lotr scrolls of middle-earth frame",
        "scrolls of middle-earth frame", "scrolls of middle-earth",
    }),
    "m21_spellbook": frozenset({
        "m21 spellbook", "m21 frame",
        "signature spellbook frame", "signature spellbook", "m21 spellbook frame",
    }),
    "phyrexia_oil": frozenset({
        "phyrexia oil", "one oil frame", "phyrexia oil frame",
        "oil frame", "phyrexian oil", "phyrexian oil frame",
    }),
    "ravnica_architecture": frozenset({
        "ravnica architecture", "ravnica architecture frame", "architecture frame",
    }),
    "sketch_frame": frozenset({
        "sketch frame", "mh2 frame", "sketch",
    }),
    "tarkir_dragon_wing": frozenset({
        "tarkir dragon wing", "tarkir dragon wing frame", "dragon wing frame",
    }),
    "theros_nyx": frozenset({
        "theros nyx", "thb frame", "nyx frame",
        "theros beyond death frame", "theros nyx frame",
    }),
    "thunder_junction_breaking_news": frozenset({
        "thunder junction breaking news",
        "breaking news frame", "breaking news", "otp frame",
    }),
    "thunder_junction_wanted_poster": frozenset({
        "thunder junction wanted poster",
        "wanted poster frame", "wanted poster", "otj frame",
    }),
    "zendikar_expeditions": frozenset({
        "zendikar expeditions", "bfz expeditions", "exp frame",
    }),
    "zendikar_hedron": frozenset({
        "zendikar hedron", "znr frame",
        "hedron frame", "zendikar rising frame", "zendikar hedron frame",
    }),
    "zendikar_rising_expeditions": frozenset({
        "zendikar rising expeditions", "znr expeditions", "zne frame",
    }),

    # ─── (3) Art — variantes de arte MPCFill ─────────────────────────────────
    "altered_art": frozenset({
        "altered art", "altered", "filtered",
    }),
    "pixel_art": frozenset({
        "pixel art", "pixelated", "pixelized",
    }),
    "popout_art": frozenset({
        "pop-out art", "popout art", "pop-out", "popout",
    }),
    "sketch_art": frozenset({
        "sketch art", "sketchified",
    }),
    "ai_art": frozenset({
        "ai art", "ai", "midjourney", "genai",
    }),
    "ai_remaster": frozenset({
        "ai remaster",
    }),
    "artist_art": frozenset({
        "artist art", "third party art", "3rd party art",
    }),
    "switched_art": frozenset({
        "switched art",
    }),
    "upscaled_scan": frozenset({
        "upscaled scan", "upscaled", "upscaled art",
        "scryfall scan", "upscaled scryfall scan",
    }),

    # ─── (4) Misc ────────────────────────────────────────────────────────────
    "nickname": frozenset({
        "nickname", "godzilla nickname", "godzilla",
    }),
    "eternal_night": frozenset({
        "eternal night card", "black & white card",
    }),
    "realistic": frozenset({
        "realistic", "realistic wotc card", "realistic card",
    }),
    "secret_lair": frozenset({
        "secret lair", "secret lair card", "sld", "sld card",
    }),
    "non_black_border": frozenset({
        "non-black border", "non-black",
        "special border", "unusual border",
    }),
    "gold_border": frozenset({
        "gold border",
        "commemorative border",
        "collectors edition border",
        "world championship deck border", "world championship border", "wc border",
        "30th anniversary edition border", "30th anniversary border", "30a border",
    }),
    "silver_border": frozenset({
        "silver border", "unset border",
    }),
    "white_border": frozenset({
        "white border", "unlimited border",
    }),

    # ─── (5) NSFW ────────────────────────────────────────────────────────────
    "nsfw": frozenset({
        "nsfw", "nsfw art",
        "not safe for work", "not safe for work art",
        "nudity", "nudity art", "gore", "gore art",
    }),

    # ─── (6) Universe (cross-property themes) ────────────────────────────────
    "anime": frozenset({
        "anime", "manga",
    }),
    "hatsune_miku": frozenset({
        "hatsune miku", "miku",
    }),
    "avatar_tla": frozenset({
        "avatar the last airbender", "avatar", "tla", "tle",
    }),
    "dr_who": frozenset({
        "dr who", "who",
    }),
    "fallout": frozenset({
        "fallout", "pip",
    }),
    "final_fantasy": frozenset({
        "final fantasy", "fin", "ff",
    }),
    "in_multiverse": frozenset({
        "in-multiverse", "mip", "uw", "universes within",
        "magic ip", "om1", "through the omenpaths",
    }),
    "league_of_legends": frozenset({
        "league of legends",
    }),
    "lord_of_the_rings": frozenset({
        "lord of the rings", "ltr", "lotr",
    }),
    "my_little_pony": frozenset({
        "my little pony", "mlp", "ponies the galloping",
    }),
    "spider_man": frozenset({
        "spider-man", "spm", "spe",
    }),
    "warhammer_40k": frozenset({
        "warhammer 40k", "40k", "warhammer",
    }),

    # ─── (7) Idioma + Meta ──────────────────────────────────────────────────
    "japanese": frozenset({
        "japanese", "jp", "jpn",
    }),
    "foil": frozenset({
        "foil", "gilded",
    }),
    "back": frozenset({
        "back", "b", "cardback", "card back",
    }),
    "token": frozenset({
        "token",
    }),
}


def _load_user_vocab_overrides() -> dict[str, frozenset[str]]:
    """Carga el vocabulario custom del usuario desde
    ``<data_dir>/tag_vocabulary.json``. Devuelve un dict con el mismo
    formato que ``_DEFAULT_TAG_VOCABULARY``.

    JSON esperado::

        {
          "aliases": {
            "gold_border": ["gold border", "gold-bordered", "gld"],
            "silver_border": ["silver border", "silver bordered", "slv"]
          }
        }

    Robusto: si el archivo no existe, está mal formado, o no tiene la
    estructura correcta, devuelve dict vacío sin crash. El log de warning
    ayuda al usuario a debuggear su JSON.
    """
    import json
    from mpc_forge import config as _cfg
    path = _cfg.PATHS.data_dir / "tag_vocabulary.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("tag_vocabulary.json inválido, ignorado: %s", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, dict):
        return {}
    out: dict[str, frozenset[str]] = {}
    for canonical, alias_list in aliases.items():
        if not isinstance(alias_list, list):
            continue
        clean = frozenset(
            str(a).lower().strip() for a in alias_list if isinstance(a, (str, int))
        )
        if clean and isinstance(canonical, str):
            out[canonical.lower().strip()] = clean
    if out:
        log.info("Vocabulario de tags custom cargado: %d tags añadidos", len(out))
    return out


# Cache module-level. Se invalida via `reload_tag_vocabulary()` desde Ajustes.
_VOCAB_CACHE: dict[str, frozenset[str]] | None = None
_ALIAS_TO_CANONICAL_CACHE: dict[str, str] | None = None


def _get_vocab() -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    """Devuelve ``(vocab_dict, alias_to_canonical_dict)`` con overrides
    del usuario aplicados si existen. Cached module-level."""
    global _VOCAB_CACHE, _ALIAS_TO_CANONICAL_CACHE
    if _VOCAB_CACHE is not None and _ALIAS_TO_CANONICAL_CACHE is not None:
        return _VOCAB_CACHE, _ALIAS_TO_CANONICAL_CACHE
    merged = dict(_DEFAULT_TAG_VOCABULARY)
    for canonical, aliases in _load_user_vocab_overrides().items():
        # Merge: si el usuario redefine un canonical existente, unimos aliases.
        existing = merged.get(canonical, frozenset())
        merged[canonical] = frozenset(set(existing) | set(aliases))
    inverse: dict[str, str] = {
        alias: canonical
        for canonical, aliases in merged.items()
        for alias in aliases
    }
    _VOCAB_CACHE, _ALIAS_TO_CANONICAL_CACHE = merged, inverse
    return merged, inverse


def reload_tag_vocabulary() -> None:
    """Fuerza recarga del vocabulario en la próxima llamada a `extract_tags`.

    Se llama desde el endpoint `POST /api/tag-vocabulary/reload` para que
    el usuario pueda editar el JSON en caliente sin reiniciar la app.
    """
    global _VOCAB_CACHE, _ALIAS_TO_CANONICAL_CACHE
    _VOCAB_CACHE = None
    _ALIAS_TO_CANONICAL_CACHE = None


# Retrocompatibilidad — algunos módulos aún importan estos símbolos.
# Se resuelven ahora vía _get_vocab() cada vez que se accede.
class _LazyAliasMap:
    """Proxy dict que resuelve al lookup real de `_get_vocab()` en cada get."""
    def get(self, key, default=None):
        return _get_vocab()[1].get(key, default)

    def __contains__(self, key):
        return key in _get_vocab()[1]

    def __getitem__(self, key):
        return _get_vocab()[1][key]


_ALIAS_TO_CANONICAL = _LazyAliasMap()


# Regex para extraer contenido de () y []. No queremos ni matchear
# recursivamente ni cruzar entre paréntesis — grupo simple con contenido no-anidado.
_BRACKET_CONTENTS_RE = re.compile(r"[\(\[]([^\(\)\[\]]+)[\)\]]")

# Regex para el prefijo de idioma MPCFill: ``{XX}`` al principio del filename,
# donde XX es un código ISO 639-1 de 2 letras. Ejemplo: ``{DE} Sol Ring.png``.
# También permite la variante ``{XX-YY}`` (locale extendido) por si aparece.
# El match es case-insensitive; siempre devolvemos el código en minúsculas.
_LANG_PREFIX_RE = re.compile(r"^\s*\{([A-Za-z]{2}(?:-[A-Za-z]{2})?)\}\s*")

# Códigos ISO 639-1 de los idiomas que Scryfall reconoce oficialmente. Fuera
# de este set no emitimos tag `lang_*` (evita basura de gente que meta
# ``{XX}`` con dos letras aleatorias). Fuente: docs de Scryfall + MPCFill.
_SUPPORTED_LANG_CODES = frozenset({
    "en", "es", "fr", "de", "it", "pt", "ja", "ko", "ru",
    "zh", "he", "la", "grc", "ar", "sa", "ph",
    "jp",  # alias no-oficial pero muy usado por MPCFill (== "ja")
})

# Nombres de carpetas especiales según MPCFill. Se comparan case-insensitive
# contra segmentos completos del folder_path.
_SPECIAL_FOLDERS: dict[str, str] = {
    "tokens": "token",       # carpeta "Tokens/" → tag `token`
    "cardbacks": "back",     # carpeta "Cardbacks/" → tag `back`
    "cardback": "back",      # variante singular tolerada
}


def extract_language(filename: str, folder_path: str = "") -> str | None:
    """Extrae el código de idioma ISO 639-1 según convención MPCFill.

    Formato: ``{XX}`` al PRINCIPIO del filename (o de algún segmento del
    folder_path). Ejemplos:
      "{DE} Sol Ring.png"                    → "de"
      "{JP} Forest [Full Art].png"           → "jp"
      "Opt.png" en "{ES}/Opt.png"            → "es"

    Devuelve ``None`` si no hay match o si el código no está en el set de
    idiomas soportados (para evitar aceptar cualquier basura).

    El prefijo de folder tiene menor prioridad que el de filename — si un
    archivo dentro de ``{ES}/`` tiene su propio ``{DE}`` al principio, gana
    ``de`` (el filename). Consistente con las guidelines: "You may specify
    a language in folder names. […] all images within the folder are
    assumed to be that language unless specified otherwise."
    """
    # 1. Filename primero (mayor prioridad)
    m = _LANG_PREFIX_RE.match(filename or "")
    if m:
        code = m.group(1).lower()
        if code in _SUPPORTED_LANG_CODES:
            return code

    # 2. Folder segments (fallback)
    if folder_path:
        for seg in re.split(r"[/\\]", folder_path):
            m = _LANG_PREFIX_RE.match(seg)
            if m:
                code = m.group(1).lower()
                if code in _SUPPORTED_LANG_CODES:
                    return code
    return None


def is_ignored_folder(folder_path: str) -> bool:
    """True si algún segmento del folder_path empieza por ``!``.

    MPCFill convención: prefijar el nombre de una carpeta con ``!`` la
    excluye del índice. Ejemplo: ``!Misc``, ``!Work in Progress``. Se
    aplica al segmento COMPLETO, no a subcadenas — ``Not!Me`` no cuenta.

    Aplica a cualquier nivel de anidamiento: si la ruta es
    ``Set1/!Backup/foo.png``, la carpeta está ignorada porque ``!Backup``
    aparece en la ruta.
    """
    if not folder_path:
        return False
    for seg in re.split(r"[/\\]", folder_path):
        if seg.strip().startswith("!"):
            return True
    return False


def detect_special_folder_tag(folder_path: str) -> str | None:
    """Devuelve un tag canónico si el folder_path contiene una carpeta
    especial MPCFill (``Tokens/`` o ``Cardbacks/``).

    Comparación case-insensitive de segmento completo. Devuelve el primer
    match encontrado, o ``None``.

    Ejemplos:
      "Tokens/Angel.png"           → "token"
      "MySet/Cardbacks/blue.png"   → "back"
      "cardbacks/red.png"          → "back"
      "MySet/Foo.png"              → None
    """
    if not folder_path:
        return None
    for seg in re.split(r"[/\\]", folder_path):
        key = seg.strip().lower()
        if key in _SPECIAL_FOLDERS:
            return _SPECIAL_FOLDERS[key]
    return None


def detect_card_type(folder_path: str) -> str:
    """Determina el card_type (CARD, CARDBACK, TOKEN) basándose **solo**
    en la carpeta contenedora, replicando la lógica de MPC Autofill.

    A diferencia de ``detect_special_folder_tag()``, esta función devuelve
    el tipo final que se almacena en ``IndexedArt.card_type``. La distinción
    es importante porque el tag ``back`` se asigna también a archivos con
    ``(B)`` en el nombre (caras traseras de DFC), pero ``card_type`` solo
    vale ``CARDBACK`` si el archivo está en una carpeta ``Cardbacks/``.

    Ejemplos:
      "Tokens/Angel.png"                → "TOKEN"
      "MySet/Cardbacks/blue.png"        → "CARDBACK"
      "MySet/Sol Ring (B).png"          → "CARD"  (no es un cardback real)
      "Full Art/Forest.png"             → "CARD"
    """
    if not folder_path:
        return "CARD"
    for seg in re.split(r"[/\\]", folder_path):
        key = seg.strip().lower()
        if key in ("cardbacks", "cardback", "card backs"):
            return "CARDBACK"
        if key in ("tokens", "token"):
            return "TOKEN"
    return "CARD"


def extract_tags(filename: str, folder_path: str = "") -> tuple[str, dict[str, bool]]:
    """Extrae tags canónicos del filename y del folder_path.

    Fuentes de tags (en orden de prioridad):
      1. Contenido dentro de ``()`` y ``[]`` en ``filename`` y ``folder_path``
         (ej. "Sol Ring (Full Art).png").
      2. Extras · F1/T4: **segmentos de folder** que coincidan EXACTAMENTE
         (case-insensitive, tras strip) con algún alias del vocabulario
         (ej. carpeta llamada "Full Art/Sol Ring.png" → tag full_art). Solo
         segmentos completos — "Anime Cards" no matchea "anime" para
         evitar falsos positivos.

    Un mismo tag detectado múltiples veces aparece una sola vez en el CSV.

    Devuelve una tupla ``(tags_csv, flags)``:
      - ``tags_csv``: string CSV ordenado alfabéticamente.
      - ``flags``: dict con las claves booleanas is_full_art, is_borderless, etc.

    Ejemplos:
      extract_tags("Sol Ring (Full Art).png")
        → ("full_art", {"is_full_art": True, ...})
      extract_tags("Forest (BL) [Retro].png")
        → ("borderless,retro", ...)
      extract_tags("Opt.png", "Anime/subfolder/Opt.png")
        → ("anime", ...)  # segmento "Anime" == alias exacto
      extract_tags("Opt.png", "Anime Cards/Opt.png")
        → ("", ...)  # "Anime Cards" != cualquier alias exacto

    Tag `back` NO se refleja como flag booleano — el indicador de reverso
    se maneja en la lógica de custom_art. Detectarlo aquí permite filtrar
    reversos en el picker si el usuario quiere.
    """
    _vocab, alias_to_canonical = _get_vocab()
    seen: set[str] = set()

    # (1) Contenido de () y [] — fuente clásica.
    for text in (filename or "", folder_path or ""):
        for content in _BRACKET_CONTENTS_RE.findall(text):
            # Un mismo bracket puede contener varios tags separados por coma:
            # "(FA, Retro)" → ["FA", "Retro"].
            for raw in content.split(","):
                key = _asciifold(raw).lower().strip()
                if not key:
                    continue
                canon = alias_to_canonical.get(key)
                if canon:
                    seen.add(canon)

    # (2) Segmentos de folder sin brackets — solo si matchean EXACTO un alias.
    # Con "/" como separador (POSIX) o "\\" (Windows). Splitteamos ambos.
    if folder_path:
        segments = re.split(r"[/\\]", folder_path)
        for seg in segments:
            key = _asciifold(seg).lower().strip()
            if not key:
                continue
            # Solo alias exactos; segmento "Full Art" matchea, "Full Art Cards" no.
            canon = alias_to_canonical.get(key)
            if canon:
                seen.add(canon)

    # (3) MPCFill · idioma con prefijo ``{XX}``. Se emite tag ``lang_XX``
    # (ej. ``lang_de``, ``lang_jp``). No pasa por el vocabulario porque los
    # códigos son un espacio abierto de 2-3 chars: los validamos contra el
    # set de idiomas soportados por Scryfall. Con esto un archivo llamado
    # ``{DE} Sol Ring.png`` queda taggeado con ``lang_de``, buscable por FTS5.
    lang = extract_language(filename, folder_path)
    if lang:
        seen.add(f"lang_{lang}")

    # (4) MPCFill · carpetas especiales ``Tokens/`` y ``Cardbacks/``. Estas
    # son convención de la comunidad: todos los archivos dentro se
    # consideran del tipo indicado. Añadimos el tag correspondiente para
    # que el picker pueda filtrarlos (o excluirlos).
    special = detect_special_folder_tag(folder_path)
    if special:
        seen.add(special)

    csv = ",".join(sorted(seen))
    # Flags derivados. Solo los que existen como columna en IndexedArt.
    flags = {
        "is_full_art":   "full_art"   in seen,
        "is_borderless": "borderless" in seen,
        "is_extended":   "extended"   in seen,
        "is_showcase":   "showcase"   in seen,
        "is_retro":      "retro"      in seen,
        "is_textless":   "textless"   in seen,
        "is_promo":      "promo"      in seen,
        "is_alt_art":    "alt_art"    in seen,
    }
    return csv, flags


# ---------------------------------------------------------------------------
# Extracción de metadatos canónicos [SET NUM]
# ---------------------------------------------------------------------------
# Los drives de MPC Autofill tienden a etiquetar los archivos con
# `[SET NUM]` (ej. "Opt [DMU 100].png", "Lightning Bolt [LEA 161]") para
# vincular sin ambigüedad el arte custom con una impresión oficial concreta
# de Scryfall. Detectarlo nos permite:
#
# - Mostrar en el picker "DMU · #100" como badge, igual que para artes
#   oficiales, aunque el archivo esté en Google Drive.
# - Que el resolver de decks priorice `[SET NUM]` sobre matching por nombre
#   fuzzy (fase 3): si el usuario tiene "Opt [DMU 100].png" y elige Opt en
#   su mazo, sabemos QUÉ impresión oficial reproduce el arte.
# - Filtrar por set en el panel de drives igual que se filtra Scryfall.
#
# Formato aceptado: `[SET NUM]` o `(SET NUM)` con:
#   - SET: 2-6 chars alfanuméricos (los códigos de set van de 2 a 6 chars).
#     Se guarda en minúsculas por consistencia con Scryfall.
#   - NUM: número + sufijos posibles ("100", "42a", "12★", "4p").
#     Se guarda tal cual (Scryfall los distingue).
#
# Detección extra:
#   - Se busca primero en el `filename`. Si no encuentra, se cae al
#     `folder_path` (una carpeta llamada "DMU 100" o "[DMU 100]" aplica
#     al arte de dentro).
#
# El `canonical_source` distingue de dónde vino el tag para debug y para
# priorizar en el picker.

# Codes de set que NO queremos matchear porque son colisiones frecuentes:
# 3 chars muy comunes en filenames de proxy art sin ser realmente set codes.
_SET_CODE_BLACKLIST = frozenset({
    "art",   # "[Art]" tag
    "back",  # "[BACK]" tag
    "fa",    # "[FA]" tag full art
    "bl",    # "[BL]" tag borderless
    "ea",    # "[EA]" tag extended
    "sc",    # "[SC]" tag showcase
    "fr",    # "[FR]" francés
    "jp",    # "[JP]" japonés
    "en",    # "[EN]" inglés
    "es",    # "[ES]" español
    "de",    # "[DE]" alemán
    "png",   # extensiones que a veces se meten
    "jpg",
    "jpeg",
    "webp",
})

# Match "[SET NUM]" o "(SET NUM)" con NUM = alphanumeric + posibles símbolos
# ★☆*p (usados por Scryfall para promo variants / stars).
# El SET es 2-6 alfanumérico, NUM es al menos 1 char.
# Requerimos un espacio entre SET y NUM para distinguir de contenidos como
# "[Full Art]" o "[BACK]" (que no llevan espacio + número).
_CANONICAL_RE = re.compile(
    r"[\[\(]"
    r"([A-Za-z0-9]{2,6})"     # set code
    r"\s+"                     # separador obligatorio
    r"([A-Za-z0-9★☆\*]+)"      # collector number (relajado)
    r"[\]\)]"
)


def extract_canonical(
    filename: str, folder_path: str = ""
) -> tuple[str | None, str | None, str]:
    """Extrae ``(expansion_code, collector_number, source)`` de un filename.

    Busca `[SET NUM]` o `(SET NUM)` primero en el ``filename``, luego en el
    ``folder_path``. Devuelve el primer match no blacklisteado.

    ``expansion_code`` se devuelve en minúsculas para consistencia con Scryfall.
    ``collector_number`` se preserva tal cual (Scryfall es case-sensitive en
    los sufijos: "12a" ≠ "12A").
    ``source`` es "filename", "folder" o "" si no hubo match.

    Ejemplos:
      "Opt [DMU 100].png"                → ("dmu", "100", "filename")
      "Sol Ring (LEA 263).png"           → ("lea", "263", "filename")
      "Lightning Bolt [2X2 117].png"     → ("2x2", "117", "filename")
      "Forest.png" en "DMU [DMU 275]/"   → ("dmu", "275", "folder")
      "Opt [BACK].png"                   → (None, None, "") (BACK blacklisted)
      "Forest [Full Art].png"            → (None, None, "") (tag conocido)
      "Random file.png"                  → (None, None, "")
    """
    for source_label, text in (("filename", filename or ""), ("folder", folder_path or "")):
        for match in _CANONICAL_RE.finditer(text):
            set_code = match.group(1).lower()
            collector_num = match.group(2)

            # Descartar tags de arte conocidos: si el contenido entero del
            # bracket es un tag reconocido en el vocabulario, NO es un par
            # (set, num). Esto evita interpretar "Full Art" o "Alt Art" como
            # `(set="full", num="Art")` — el falso match más común.
            full_content = f"{set_code} {collector_num}".lower()
            if _ALIAS_TO_CANONICAL.get(full_content):
                continue
            # Si el 'collector_number' es puramente alfabético y matchea la
            # segunda palabra de algún alias conocido, también es probable
            # colisión. Ej.: "Full Art" → SET="Full", NUM="Art". "Art" solo
            # no está en el vocab, pero la combinación sí. Ya lo cubre el
            # check anterior.

            if set_code in _SET_CODE_BLACKLIST:
                continue
            # Extra sanity: los set codes de Scryfall siempre son alfanuméricos
            # pero raramente son todo-dígitos y cortos. Descartamos "1 2",
            # "10 5" y similares (típicos en "Nave Marín 1 2" tipo página).
            if set_code.isdigit() and len(set_code) < 3:
                continue
            # Y el collector_number nunca es puramente alfabético largo — si
            # es todo letras (>2 chars), casi seguro es una etiqueta tipo
            # "Alternate", "Retro", etc. Los números reales de Scryfall
            # siempre tienen al menos un dígito.
            if collector_num.isalpha() and len(collector_num) > 2:
                continue
            return set_code, collector_num, source_label
    return None, None, ""


# ---------------------------------------------------------------------------
# Google Drive API v3 (modo principal, requiere API key)
# ---------------------------------------------------------------------------

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
_PAGE_SIZE = 1000  # máximo permitido por la API
_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
}
# Fields mínimos que necesitamos por archivo:
_FIELDS = "nextPageToken,files(id,name,mimeType,size,parents,shortcutDetails)"


@dataclass
class IndexResult:
    source_id: int
    files_added: int
    files_updated: int
    folders_visited: int
    error: str | None = None
    used_api_key: bool = False


async def _drive_api_list(
    client: httpx.AsyncClient,
    folder_id: str,
    api_key: str,
    only_images: bool = True,
) -> list[dict]:
    """Lista todos los hijos directos de una carpeta (imágenes y subcarpetas).

    Pagina con nextPageToken hasta agotar. Devuelve lista de dicts con:
    {id, name, mimeType, size?, parents?, shortcutDetails?}
    """
    if only_images:
        q = (
            f"'{folder_id}' in parents and trashed=false and ("
            "mimeType='application/vnd.google-apps.folder' or "
            "mimeType contains 'image/'"
            ")"
        )
    else:
        q = f"'{folder_id}' in parents and trashed=false"

    out: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "q": q,
            "pageSize": str(_PAGE_SIZE),
            "fields": _FIELDS,
            "key": api_key,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        r = await client.get(f"{_DRIVE_API_BASE}/files", params=params, timeout=30.0)
        r.raise_for_status()
        payload = r.json()
        out.extend(payload.get("files", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return out


async def _index_via_api(
    db: AsyncSession,
    source: ArtSource,
    folder_id: str,
    api_key: str,
    on_progress=None,
) -> IndexResult:
    """Indexa un drive recursivamente usando la API v3.

    Recorre subcarpetas en BFS (una capa a la vez para no explotar la pila).
    Guarda cada imagen con su ruta relativa desde la raíz.

    Commits parciales cada 500 filas: si el proceso se corta o el usuario
    consulta durante el indexado, ve el progreso real, no todo o nada.
    Log periódico para poder ver el avance en el fichero de log.
    """
    from sqlalchemy import func
    files_added = 0
    files_updated = 0
    folders_visited = 0
    since_last_commit = 0
    _LOG_FOLDERS_EVERY = 20

    # Cola: (folder_id, path_relativo)
    queue: list[tuple[str, str]] = [(folder_id, "")]
    seen_folders: set[str] = {folder_id}

    # Preload de IndexedArt existentes para este source. Antes: 1 SELECT por
    # cada archivo del drive (miles). Ahora: 1 SELECT total + lookups O(1) en
    # el dict. Las filas nuevas insertadas durante el indexado se añaden aquí
    # para mantenerlo consistente si el mismo file_id aparece dos veces.
    existing_by_file_id: dict[str, IndexedArt] = {
        art.file_id: art
        for art in (await db.scalars(
            select(IndexedArt).where(IndexedArt.source_id == source.id)
        )).all()
    }

    async def _partial_commit() -> None:
        """Persiste lo acumulado y actualiza indexed_files para la UI."""
        nonlocal since_last_commit
        source.indexed_files = int(await db.scalar(
            select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source.id)
        ) or 0)
        await db.commit()
        since_last_commit = 0
        # Notificar progreso al caller (IndexQueue o quien sea)
        if on_progress:
            on_progress(
                files_added=files_added,
                files_updated=files_updated,
                folders_visited=folders_visited,
                files_total=source.indexed_files,
            )

    async with httpx.AsyncClient(verify=not ssl_insecure()) as client:
        while queue:
            current_id, current_path = queue.pop(0)
            folders_visited += 1

            if folders_visited % _LOG_FOLDERS_EVERY == 0:
                log.info(
                    "  [%s] %d folders visitados, +%d archivos hasta ahora "
                    "(cola: %d folders pendientes)",
                    source.name, folders_visited, files_added, len(queue),
                )

            try:
                items = await _drive_api_list(client, current_id, api_key)
            except httpx.HTTPStatusError as e:
                # Un 403/404 en una subcarpeta no debe abortar todo el drive.
                # Solo abortamos si es en la raíz o es un error de auth.
                if e.response.status_code in (401, 403) and current_id == folder_id:
                    # Persistir lo acumulado antes de salir
                    if since_last_commit > 0:
                        await _partial_commit()
                    return IndexResult(
                        source_id=source.id, files_added=files_added,
                        files_updated=files_updated,
                        folders_visited=folders_visited,
                        error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                        used_api_key=True,
                    )
                log.warning("Saltando subcarpeta %s (%s): %s", current_id, current_path, e)
                continue

            for item in items:
                mime = item.get("mimeType", "")
                item_id = item.get("id")
                name = item.get("name", "")

                # Resolver shortcuts a su target si es un shortcut a un folder o imagen
                if mime == "application/vnd.google-apps.shortcut":
                    sc = item.get("shortcutDetails") or {}
                    target_id = sc.get("targetId")
                    target_mime = sc.get("targetMimeType", "")
                    if not target_id:
                        continue
                    item_id = target_id
                    mime = target_mime

                if mime == "application/vnd.google-apps.folder":
                    # MPCFill · convención de exclusión: carpetas con prefijo
                    # ``!`` no se indexan. Cortamos antes de encolarlas para
                    # ahorrar la request de listing.
                    if name.strip().startswith("!"):
                        log.debug("Skip carpeta ignorada por prefijo '!': %s", name)
                        continue
                    if item_id and item_id not in seen_folders:
                        seen_folders.add(item_id)
                        subpath = f"{current_path}/{name}" if current_path else name
                        queue.append((item_id, subpath))
                    continue

                if mime not in _IMAGE_MIMES:
                    continue

                existing = existing_by_file_id.get(item_id)

                size = int(item.get("size", 0) or 0)
                tags_csv, tag_flags = extract_tags(name, current_path)
                exp_code, coll_num, canon_source = extract_canonical(name, current_path)
                if existing:
                    existing.filename = name
                    existing.name_normalized = normalize_filename(name)
                    existing.folder_path = current_path
                    existing.size_bytes = size
                    existing.mime_type = mime
                    existing.indexed_at = datetime.now(timezone.utc)
                    existing.tags = tags_csv
                    existing.expansion_code = exp_code
                    existing.collector_number = coll_num
                    existing.canonical_source = canon_source
                    existing.card_type = detect_card_type(current_path)
                    for flag, value in tag_flags.items():
                        setattr(existing, flag, value)
                    files_updated += 1
                else:
                    new_art = IndexedArt(
                        source_id=source.id,
                        file_id=item_id,
                        filename=name,
                        name_normalized=normalize_filename(name),
                        folder_path=current_path,
                        size_bytes=size,
                        mime_type=mime,
                        tags=tags_csv,
                        expansion_code=exp_code,
                        collector_number=coll_num,
                        canonical_source=canon_source,
                        card_type=detect_card_type(current_path),
                        **tag_flags,
                    )
                    db.add(new_art)
                    existing_by_file_id[item_id] = new_art
                    files_added += 1
                since_last_commit += 1

                # Commit parcial: la UI ve progreso y no perdemos datos si
                # algo va mal.
                if since_last_commit >= _COMMIT_EVERY:
                    await _partial_commit()

    # Commit final del residuo
    if since_last_commit > 0:
        await _partial_commit()

    return IndexResult(
        source_id=source.id, files_added=files_added, files_updated=files_updated,
        folders_visited=folders_visited, used_api_key=True,
    )


# ---------------------------------------------------------------------------
# Fallback: scraping de embeddedfolderview
# ---------------------------------------------------------------------------

# El HTML de embeddedfolderview incluye scripts con datos como:
# {"data":[["FILE_ID","file",...,"NAME",...]]} — se puede regexear.
# Es frágil pero funciona hoy (comprobado). Solo devuelve el primer nivel.
_EMBED_ITEM_RE = re.compile(
    r'"([A-Za-z0-9_\-]{20,})"[^"]*?"application/[^"]+/([^"]+)"[^"]*?"([^"]+\.(?:png|jpe?g|webp|gif))"',
    re.IGNORECASE,
)


async def _index_via_scraping(
    db: AsyncSession,
    source: ArtSource,
    folder_id: str,
    on_progress=None,
) -> IndexResult:
    """Modo pobre sin API key. Solo indexa el primer nivel (sin subcarpetas)
    y sin tamaño/mime fiable. Advierte al usuario en el error message si
    detectamos que el drive es muy grande.
    """
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    files_added = 0
    files_updated = 0

    async with httpx.AsyncClient(verify=not ssl_insecure(), follow_redirects=True) as client:
        try:
            r = await client.get(url, timeout=30.0, headers={
                "User-Agent": "Mozilla/5.0 (compatible; MPC-Forge indexer)",
            })
            r.raise_for_status()
            html = r.text
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            return IndexResult(
                source_id=source.id, files_added=0, files_updated=0,
                folders_visited=0,
                error=f"No se pudo cargar embedded view: {e}",
            )

    matches = _EMBED_ITEM_RE.findall(html)
    if matches:
        existing_by_file_id: dict[str, IndexedArt] = {
            art.file_id: art
            for art in (await db.scalars(
                select(IndexedArt).where(IndexedArt.source_id == source.id)
            )).all()
        }
    else:
        existing_by_file_id = {}

    for file_id, mime_frag, name in matches:
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            continue
        mime = f"image/{mime_frag.split('/')[-1]}"
        tags_csv, tag_flags = extract_tags(name, "")
        exp_code, coll_num, canon_source = extract_canonical(name, "")
        existing = existing_by_file_id.get(file_id)
        if existing:
            existing.filename = name
            existing.name_normalized = normalize_filename(name)
            existing.mime_type = mime
            existing.indexed_at = datetime.now(timezone.utc)
            existing.tags = tags_csv
            existing.expansion_code = exp_code
            existing.collector_number = coll_num
            existing.canonical_source = canon_source
            existing.card_type = detect_card_type("")
            for flag, value in tag_flags.items():
                setattr(existing, flag, value)
            files_updated += 1
        else:
            new_art = IndexedArt(
                source_id=source.id, file_id=file_id, filename=name,
                name_normalized=normalize_filename(name),
                folder_path="", size_bytes=0, mime_type=mime,
                tags=tags_csv,
                expansion_code=exp_code,
                collector_number=coll_num,
                canonical_source=canon_source,
                card_type=detect_card_type(""),
                **tag_flags,
            )
            db.add(new_art)
            existing_by_file_id[file_id] = new_art
            files_added += 1

    err = None
    if files_added + files_updated == 0:
        err = (
            "No se encontraron imágenes en la vista pública. "
            "Puede ser que el drive tenga estructura profunda (sin API key solo "
            "indexamos el primer nivel), o que la carpeta ya no sea pública."
        )
    if on_progress:
        on_progress(
            files_added=files_added, files_updated=files_updated,
            folders_visited=1, files_total=files_added + files_updated,
        )
    return IndexResult(
        source_id=source.id, files_added=files_added, files_updated=files_updated,
        folders_visited=1, error=err,
    )


# ---------------------------------------------------------------------------
# API pública del módulo
# ---------------------------------------------------------------------------

_FOLDER_ID_RE = re.compile(r"folders/([A-Za-z0-9_\-]+)")


def _extract_folder_id(url: str) -> str | None:
    m = _FOLDER_ID_RE.search(url)
    return m.group(1) if m else None


async def index_source(
    db: AsyncSession,
    source_id: int,
    on_progress=None,
) -> IndexResult:
    """Indexa una source. El comportamiento depende del ``source_type``:

    - ``"gdrive"``: API v3 si hay API key, si no scraping HTML (legacy path).
    - ``"local-folder"``, ``"http-listing"``, cualquier otro tipo registrado
      en ``services.source_types``: usa el flujo genérico (`_index_generic`)
      que consume el ``SourceFile`` iterado por el tipo.
    - ``"gdrive-file"`` o cualquier tipo sin implementación: no-op con
      warning.

    Serializa vía semáforo global: aunque la UI lance N indexados en paralelo,
    se ejecutan uno a uno para no saturar el lock de SQLite.

    Es una operación potencialmente larga (segundos a minutos para drives
    grandes). Debe llamarse desde una BackgroundTask, no bloqueando la request.

    Args:
        on_progress: callback opcional invocado tras cada commit parcial.
            Signatura: ``(files_added, files_updated, folders_visited, files_total) -> None``.
            Permite que el caller (ej. ``IndexQueue``) actualice su estado
            de progreso en tiempo real sin acoplar el indexer a la cola.
    """
    source = await db.get(ArtSource, source_id)
    if not source:
        return IndexResult(source_id=source_id, files_added=0, files_updated=0,
                          folders_visited=0, error="Source no encontrado")

    stype = source.source_type or "gdrive"

    # gdrive → path legacy específico. Los demás → path genérico via source_types.
    if stype == "gdrive":
        folder_id = _extract_folder_id(source.url)
        if not folder_id:
            result = IndexResult(
                source_id=source_id, files_added=0, files_updated=0,
                folders_visited=0,
                error="La URL no parece un folder de Google Drive",
            )
            source.indexed_at = datetime.now(timezone.utc)
            source.index_error = result.error or ""
            await db.commit()
            return result

        async with _INDEX_SEMAPHORE:  # máx 3 concurrentes
            api_key = (getattr(cfg, "GOOGLE_API_KEY", "") or "").strip()
            mode = "API v3" if api_key else "scraping (sin API key)"
            log.info("▶ Empezando indexado de source %d (%s) vía %s",
                     source_id, source.name, mode)
            if api_key:
                result = await _index_via_api(db, source, folder_id, api_key, on_progress=on_progress)
            else:
                result = await _index_via_scraping(db, source, folder_id, on_progress=on_progress)
    elif stype == "gdrive-file":
        # File source individual — no se indexa (es un solo archivo, se usa
        # directo al añadir arte custom por URL).
        result = IndexResult(
            source_id=source_id, files_added=0, files_updated=0,
            folders_visited=0,
            error="Los sources tipo 'archivo suelto' no se indexan.",
        )
    else:
        # Dispatch al flujo genérico basado en source_types.
        from mpc_forge.services.source_types import resolve
        type_cls = resolve(stype)
        if type_cls is None:
            result = IndexResult(
                source_id=source_id, files_added=0, files_updated=0,
                folders_visited=0,
                error=f"Tipo de source desconocido: {stype!r}",
            )
        else:
            async with _INDEX_SEMAPHORE:
                log.info("▶ Empezando indexado genérico de source %d (%s, tipo=%s)",
                         source_id, source.name, stype)
                result = await _index_generic(db, source, type_cls, on_progress=on_progress)

    # Actualizar estado del source. Contamos filas reales de IndexedArt.
    from sqlalchemy import func
    source.indexed_at = datetime.now(timezone.utc)
    source.indexed_files = int(await db.scalar(
        select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source.id)
    ) or 0)
    source.index_error = result.error or ""
    await db.commit()

    log.info(
        "✓ Terminado source %d (%s, tipo=%s): total=%d archivos (+%d nuevos, "
        "~%d actualizados) en %d folders. Error=%s",
        source_id, source.name, stype, source.indexed_files, result.files_added,
        result.files_updated, result.folders_visited, result.error or "ninguno",
    )
    return result


async def _index_generic(db: AsyncSession, source: ArtSource, type_cls, on_progress=None) -> IndexResult:
    """Flujo de indexado genérico para tipos que exponen ``list_files()``.

    Consume el ``AsyncIterator[SourceFile]`` de ``type_cls`` y hace upsert en
    ``IndexedArt`` para cada archivo. Comparte con el path gdrive:
      - Extracción de tags y metadatos canónicos.
      - Normalización con asciifolding.
      - Commits parciales cada ``_COMMIT_EVERY`` filas para no bloquear
        el lock demasiado tiempo con carpetas gigantes.
      - Extras · F2/T8: cálculo pHash inline si `phash.enabled` está
        activo. NO se hace en el path gdrive (demasiadas descargas) —
        para gdrive se ofrece un endpoint dedicado en `routes/integrations.py`.
    """
    from mpc_forge.services import phash as _phash

    # ¿Debemos calcular pHash inline? Solo si:
    #   - El setting phash.enabled está activo (mira BD)
    #   - Las libs Pillow/imagehash están disponibles
    # La evaluación se hace UNA vez al arrancar el indexado.
    phash_active = await _phash.enabled(db)
    # Crear cliente httpx propio si vamos a calcular pHash. Reutilizado para
    # todas las descargas del batch. Se cierra al final via context manager.
    phash_client = None
    if phash_active:
        import httpx
        from mpc_forge.ssl_config import ssl_insecure
        from mpc_forge import config as _cfg
        phash_client = httpx.AsyncClient(
            timeout=15.0,
            verify=not ssl_insecure(),
            headers={"User-Agent": _cfg.MOXFIELD_USER_AGENT},
        )

    files_added = 0
    files_updated = 0
    since_last_commit = 0

    # Preload de IndexedArt existentes: cambia N SELECTs (uno por archivo del
    # source) por 1 SELECT + lookup O(1) en memoria.
    existing_by_file_id: dict[str, IndexedArt] = {
        art.file_id: art
        for art in (await db.scalars(
            select(IndexedArt).where(IndexedArt.source_id == source.id)
        )).all()
    }

    async def _partial_commit():
        nonlocal since_last_commit
        try:
            await db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("Commit parcial de source %d falló: %s", source.id, e)
        since_last_commit = 0
        if on_progress:
            on_progress(
                files_added=files_added, files_updated=files_updated,
                folders_visited=0, files_total=files_added + files_updated,
            )

    try:
        async for sf in type_cls.list_files(source):
            # MPCFill · convención de exclusión: si algún segmento del
            # folder_path empieza por ``!``, saltamos el archivo. Los tipos
            # de source genéricos no conocen la convención, así que la
            # aplicamos post-listing para uniformidad con el flujo de gdrive.
            if is_ignored_folder(sf.folder_path):
                continue
            existing = existing_by_file_id.get(sf.file_id)

            tags_csv, tag_flags = extract_tags(sf.filename, sf.folder_path)
            exp_code, coll_num, canon_source = extract_canonical(sf.filename, sf.folder_path)

            # URLs específicas del tipo de source. Para gdrive las columnas
            # quedan NULL (se generan al vuelo con _thumb_url/_download_url en
            # gdrive_search). Para los demás tipos, se almacenan aquí para que
            # gdrive_search las use en lugar del formato hardcodeado de Drive.
            try:
                _dl_url = type_cls.download_url(source, sf.file_id)
            except (NotImplementedError, Exception):  # noqa: BLE001
                _dl_url = None
            try:
                _th_url = type_cls.thumbnail_url(source, sf.file_id)
            except (NotImplementedError, Exception):  # noqa: BLE001
                _th_url = None

            if existing:
                existing.filename = sf.filename
                existing.name_normalized = normalize_filename(sf.filename)
                existing.folder_path = sf.folder_path
                existing.size_bytes = sf.size_bytes
                existing.mime_type = sf.mime_type
                existing.indexed_at = datetime.now(timezone.utc)
                existing.tags = tags_csv
                existing.expansion_code = exp_code
                existing.collector_number = coll_num
                existing.canonical_source = canon_source
                existing.card_type = detect_card_type(sf.folder_path)
                existing.download_url = _dl_url
                existing.thumb_url = _th_url
                for flag, value in tag_flags.items():
                    setattr(existing, flag, value)
                if phash_active and phash_client is not None and not existing.image_hash:
                    thumb = _phash._default_thumb_url(existing, source)
                    if thumb:
                        h = await _phash.compute_from_url(phash_client, thumb)
                        if h:
                            existing.image_hash = h
                files_updated += 1
            else:
                new_art = IndexedArt(
                    source_id=source.id,
                    file_id=sf.file_id,
                    filename=sf.filename,
                    name_normalized=normalize_filename(sf.filename),
                    folder_path=sf.folder_path,
                    size_bytes=sf.size_bytes,
                    mime_type=sf.mime_type,
                    tags=tags_csv,
                    expansion_code=exp_code,
                    collector_number=coll_num,
                    canonical_source=canon_source,
                    card_type=detect_card_type(sf.folder_path),
                    download_url=_dl_url,
                    thumb_url=_th_url,
                    **tag_flags,
                )
                if phash_active and phash_client is not None:
                    thumb = _phash._default_thumb_url(new_art, source)
                    if thumb:
                        h = await _phash.compute_from_url(phash_client, thumb)
                        if h:
                            new_art.image_hash = h
                db.add(new_art)
                existing_by_file_id[sf.file_id] = new_art
                files_added += 1
            since_last_commit += 1
            if since_last_commit >= _COMMIT_EVERY:
                await _partial_commit()

        if since_last_commit > 0:
            await _partial_commit()
    except Exception as e:  # noqa: BLE001
        log.exception("Error indexando source %d (%s) via tipo genérico",
                      source.id, source.name)
        return IndexResult(
            source_id=source.id, files_added=files_added, files_updated=files_updated,
            folders_visited=0, error=f"{type(e).__name__}: {e}",
        )
    finally:
        if phash_client is not None:
            await phash_client.aclose()

    return IndexResult(
        source_id=source.id, files_added=files_added, files_updated=files_updated,
        folders_visited=0, error=None,
    )


async def clear_index(db: AsyncSession, source_id: int) -> int:
    """Borra todo el índice de un source. Devuelve nº de filas borradas."""
    from sqlalchemy import func
    n = int(await db.scalar(
        select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source_id)
    ) or 0)
    await db.execute(delete(IndexedArt).where(IndexedArt.source_id == source_id))
    source = await db.get(ArtSource, source_id)
    if source:
        source.indexed_at = None
        source.indexed_files = 0
        source.index_error = ""
    await db.commit()
    return n


# ---------------------------------------------------------------------------
# Backfill de nombres normalizados
# ---------------------------------------------------------------------------

_NORMALIZATION_VERSION_KEY = "gdrive.normalization_version"


async def backfill_normalized_names(db: AsyncSession) -> int:
    """Recalcula ``name_normalized`` y ``tags``/flags si la versión cambió.

    Se ejecuta al arrancar (desde ``lifespan`` en ``app.py``). Compara la
    versión guardada en ``KeyValue`` con ``NORMALIZATION_VERSION``. Si difieren
    (o si nunca se ejecutó), reprocesa TODAS las filas en batches de 1000 y
    actualiza en su sitio, sin borrar el índice ni exigir al usuario reindexar.

    Idempotente: ejecutarlo dos veces no cambia nada si la versión está al día.

    Qué se actualiza:
      - ``name_normalized``: aplica la pipeline actual (con asciifolding
        desde v2).
      - ``tags`` y flags booleanos (``is_full_art``, ``is_borderless``, …):
        extraídos del filename y del folder_path (desde v3).

    Rendimiento: para 500k filas, ~5 segundos. Corremos en background dentro
    del lifespan para no bloquear el arranque de la UI.

    Devuelve el número de filas actualizadas (0 si no había cambio o índice
    vacío).
    """
    from mpc_forge.models import KeyValue

    # ¿Ya está en la versión actual?
    kv = await db.get(KeyValue, _NORMALIZATION_VERSION_KEY)
    try:
        current = int(kv.value) if kv else 0
    except (ValueError, AttributeError):
        current = 0
    if current >= NORMALIZATION_VERSION:
        return 0

    total = int(await db.scalar(
        select(__import__("sqlalchemy").func.count(IndexedArt.id))
    ) or 0)
    if total == 0:
        # Índice vacío — marcamos la versión y salimos.
        if kv:
            kv.value = str(NORMALIZATION_VERSION)
        else:
            db.add(KeyValue(key=_NORMALIZATION_VERSION_KEY, value=str(NORMALIZATION_VERSION)))
        await db.commit()
        return 0

    log.info(
        "Backfill de normalización: reprocesando %d filas (v%d → v%d)…",
        total, current, NORMALIZATION_VERSION,
    )
    updated = 0
    batch_size = 1000
    offset = 0
    while offset < total:
        rows = (await db.scalars(
            select(IndexedArt)
            .order_by(IndexedArt.id)
            .offset(offset)
            .limit(batch_size)
        )).all()
        if not rows:
            break
        for art in rows:
            new_norm = normalize_filename(art.filename)
            new_tags_csv, new_flags = extract_tags(art.filename, art.folder_path)
            new_exp, new_num, new_canon_source = extract_canonical(
                art.filename, art.folder_path
            )
            row_changed = False
            if new_norm != art.name_normalized:
                art.name_normalized = new_norm
                row_changed = True
            if new_tags_csv != (art.tags or ""):
                art.tags = new_tags_csv
                row_changed = True
            for flag, value in new_flags.items():
                if getattr(art, flag, False) != value:
                    setattr(art, flag, value)
                    row_changed = True
            # Canonical metadata (v4). None es un valor legítimo (arte sin tag).
            if new_exp != art.expansion_code:
                art.expansion_code = new_exp
                row_changed = True
            if new_num != art.collector_number:
                art.collector_number = new_num
                row_changed = True
            if new_canon_source != (art.canonical_source or ""):
                art.canonical_source = new_canon_source
                row_changed = True
            # card_type (v6): determinado por carpeta, no por tags del filename.
            new_card_type = detect_card_type(art.folder_path)
            if new_card_type != (art.card_type or "CARD"):
                art.card_type = new_card_type
                row_changed = True
            if row_changed:
                updated += 1
        await db.commit()
        offset += batch_size

    # Registramos la versión completada.
    kv = await db.get(KeyValue, _NORMALIZATION_VERSION_KEY)
    if kv:
        kv.value = str(NORMALIZATION_VERSION)
    else:
        db.add(KeyValue(key=_NORMALIZATION_VERSION_KEY, value=str(NORMALIZATION_VERSION)))
    await db.commit()
    log.info("Backfill de normalización completado: %d filas actualizadas de %d totales",
             updated, total)
    return updated
