from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")

SQLITE_IN_CHUNK = 500


def chunked(items: Iterable[T], size: int = SQLITE_IN_CHUNK) -> Iterator[list[T]]:
    seq = list(items)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
