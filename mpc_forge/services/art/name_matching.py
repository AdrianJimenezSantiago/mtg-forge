"""Claves para emparejar nombres de carta con nombres de fichero.

Un fichero no puede llamarse exactamente como muchas cartas: Windows prohíbe
`:` o `/`, los teclados no siempre tienen `û` o `Æ` y cada cual separa las
palabras como quiere. `match_key` reduce un nombre a una forma canónica que
sobrevive a todo eso, y se usa a los dos lados (al indexar el fichero y al
buscar la carta), así que basta con que ambos produzcan la misma clave.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

_LIGATURES = str.maketrans({"Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "ß": "ss"})
_APOSTROPHES_RE = re.compile(r"['’‘`´]")
_SEPARATORS_RE = re.compile(r"[\W_]+")
_FACE_SPLIT = " // "
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.translate(_LIGATURES))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@lru_cache(maxsize=8192)
def match_key(name: str) -> str:
    """Forma canónica de un nombre: sin tildes, sin puntuación y en minúsculas.

    `Lim-Dûl's Vault`, `lim dul's vault` y `Lim_Duls_Vault` dan la misma clave.
    Si el nombre es solo puntuación (hay cartas así) se conserva tal cual en
    minúsculas para que la clave nunca quede vacía.
    """
    folded = _APOSTROPHES_RE.sub("", _fold(name)).casefold()
    key = _SEPARATORS_RE.sub(" ", folded).strip()
    return key or " ".join(name.casefold().split())


@dataclass(frozen=True)
class CardKeys:
    """Claves con las que se reconoce cada cara de una carta.

    `front` incluye el nombre completo y el de la cara frontal; `back`, el de
    la cara trasera de una carta de dos caras (vacío en el resto).
    """

    front: frozenset[str]
    back: frozenset[str]

    @property
    def all(self) -> frozenset[str]:
        return self.front | self.back


@lru_cache(maxsize=4096)
def card_keys(name: str, *alt_names: str | None) -> CardKeys:
    """Claves de una carta a partir de su nombre y de nombres alternativos.

    Los alternativos son, por ejemplo, el nombre impreso de una edición en otro
    idioma: se aceptan con la misma separación por caras que el principal. Un
    paréntesis final (`B.F.M. (Big Furry Monster)`) también se acepta sin él,
    porque al leer el fichero se toma como variante.
    """
    front: set[str] = set()
    back: set[str] = set()
    for raw in (name, *alt_names):
        if not raw or not raw.strip():
            continue
        front.add(match_key(raw))
        bare = _TRAILING_PAREN_RE.sub("", raw).strip()
        if bare and bare != raw.strip():
            front.add(match_key(bare))
        faces = [f for f in (p.strip() for p in raw.split(_FACE_SPLIT)) if f]
        if len(faces) > 1:
            front.add(match_key(faces[0]))
            back.update(match_key(f) for f in faces[1:])
    back -= front
    return CardKeys(front=frozenset(front), back=frozenset(back))
