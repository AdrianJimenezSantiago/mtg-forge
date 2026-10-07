from __future__ import annotations

import re
from typing import Any

_SECTION_ALIASES: dict[str, str] = {
    "commander": "commander",
    "commanders": "commander",
    "companion": "companion",
    "companions": "companion",
    "mainboard": "mainboard",
    "main": "mainboard",
    "deck": "mainboard",
    "sideboard": "sideboard",
    "side": "sideboard",
    "maybeboard": "maybeboard",
    "maybe": "maybeboard",
    "tokens": "tokens",
}


def _detect_section_header(line: str) -> str | None:
    s = line.strip()
    if not s:
        return None
    if s.startswith("//"):
        candidate = s.lstrip("/").strip().lower()
        candidate = candidate.split("(", 1)[0].strip()
        return _SECTION_ALIASES.get(candidate)
    m = re.match(r"^([A-Za-z]+)(?:\s*\(\d+\))?\s*$", s)
    if m:
        return _SECTION_ALIASES.get(m.group(1).lower())
    return None


def parse_plain_decklist(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    line_re = re.compile(
        r"^\s*(?P<qty>\d+)?\s*[xX]?\s+"
        r"(?P<name>[^\(\[\n]+?)"
        r"(?:\s+[\(\[](?P<set>[A-Za-z0-9]{2,6})[\)\]]"
        r"\s*(?P<num>\S+)?)?\s*$"
    )
    sb_prefix_re = re.compile(r"^\s*SB:\s*", re.IGNORECASE)

    current_role = "mainboard"

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        section = _detect_section_header(raw)
        if section is not None:
            current_role = section
            continue

        role_for_this_line = current_role
        if sb_prefix_re.match(raw):
            role_for_this_line = "sideboard"
            raw_clean = sb_prefix_re.sub("", raw)
        else:
            raw_clean = raw

        m = line_re.match(raw_clean)
        if not m:
            entries.append(
                {
                    "name": raw_clean.strip(),
                    "quantity": 1,
                    "set": None,
                    "number": None,
                    "role": role_for_this_line,
                    "raw_line": raw,
                }
            )
            continue
        entries.append(
            {
                "name": m.group("name").strip(),
                "quantity": int(m.group("qty") or 1),
                "set": (m.group("set") or "").lower() or None,
                "number": m.group("num") or None,
                "role": role_for_this_line,
                "raw_line": raw,
            }
        )
    return entries
