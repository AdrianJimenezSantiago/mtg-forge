from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BuildProgress:
    deck_id: int
    total: int = 0
    current: int = 0
    current_name: str = ""
    kind: str = "xml"
    started_at: float = field(default_factory=time.time)
    done: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        elapsed = time.time() - self.started_at
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


_progress: dict[int, BuildProgress] = {}


def start(deck_id: int, total: int, kind: str = "xml") -> BuildProgress:
    p = BuildProgress(deck_id=deck_id, total=total, kind=kind)
    _progress[deck_id] = p
    return p


def tick(deck_id: int, card_name: str) -> None:
    p = _progress.get(deck_id)
    if p is None or p.done:
        return
    p.current += 1
    p.current_name = card_name


def finish(deck_id: int, error: str | None = None) -> None:
    p = _progress.get(deck_id)
    if p is None:
        return
    p.done = True
    p.error = error
    p.current_name = ""


def get(deck_id: int) -> BuildProgress | None:
    return _progress.get(deck_id)


def clear(deck_id: int) -> None:
    _progress.pop(deck_id, None)
