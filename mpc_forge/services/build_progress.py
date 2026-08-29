"""Estado de progreso para generación de XML/PDF.

Un dict en memoria indexado por ``deck_id`` que refleja qué carta se está
descargando ahora mismo. El frontend hace polling a ``GET /api/decks/{id}
/build-progress`` mientras el POST ``/build-xml`` está pendiente, y usa esos
datos para pintar una barra de progreso real con el nombre de la carta actual.

La app corre en single-process, así que un dict local basta — no hace falta
persistir en BD ni compartir entre workers. Si en algún momento se despliega
detrás de gunicorn con >1 worker, este módulo necesita cambiar a Redis o BD.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BuildProgress:
    """Snapshot del progreso de un build en curso o recién terminado."""
    deck_id: int
    total: int = 0
    current: int = 0
    current_name: str = ""
    kind: str = "xml"  # "xml" | "pdf"
    started_at: float = field(default_factory=time.time)
    done: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        elapsed = time.time() - self.started_at
        # Estimación simple de tiempo restante: media de tiempo por carta.
        eta_seconds: float | None = None
        if self.current > 0 and not self.done and self.total > 0:
            per_card = elapsed / self.current
            remaining = self.total - self.current
            eta_seconds = per_card * remaining
        return {
            "deck_id": self.deck_id,
            "total": self.total,
            "current": self.current,
            "current_name": self.current_name,
            "kind": self.kind,
            "done": self.done,
            "error": self.error,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
            "percent": round(100 * self.current / self.total, 1) if self.total > 0 else 0,
        }


# Registro global. Cada build sobrescribe la entrada anterior del mismo deck_id.
_progress: dict[int, BuildProgress] = {}


def start(deck_id: int, total: int, kind: str = "xml") -> BuildProgress:
    """Arranca un tracker limpio para el build. Descarta cualquier estado
    anterior del mismo mazo (asumimos: no builds concurrentes por deck)."""
    p = BuildProgress(deck_id=deck_id, total=total, kind=kind)
    _progress[deck_id] = p
    return p


def tick(deck_id: int, card_name: str) -> None:
    """Incrementa el contador y actualiza el nombre de la carta actual.
    Silent-fail si no hay build iniciado — no queremos romper el build por
    un fallo en el tracking."""
    p = _progress.get(deck_id)
    if p is None or p.done:
        return
    p.current += 1
    p.current_name = card_name


def finish(deck_id: int, error: str | None = None) -> None:
    """Marca el build como terminado. La entrada se conserva unos segundos
    para que el frontend pueda leer el estado final antes de descartarla."""
    p = _progress.get(deck_id)
    if p is None:
        return
    p.done = True
    p.error = error
    p.current_name = ""


def get(deck_id: int) -> BuildProgress | None:
    """Estado actual, o None si no hay build para ese mazo."""
    return _progress.get(deck_id)


def clear(deck_id: int) -> None:
    """Descarta el estado explícitamente. El frontend lo llama tras confirmar
    que ya leyó el estado final ``done=True``."""
    _progress.pop(deck_id, None)
