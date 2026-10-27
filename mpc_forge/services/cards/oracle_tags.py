from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.config import SCRYFALL_USER_AGENT
from mpc_forge.models import BulkSyncState, OracleRole
from mpc_forge.services.cards import bulk_data
from mpc_forge.ssl_config import ssl_insecure
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)

KIND = "oracle_tags"


@dataclass(frozen=True)
class Role:
    tags: tuple[str, ...]
    exclude_tags: tuple[str, ...] = ()
    minus_roles: tuple[str, ...] = ()


ROLES: dict[str, Role] = {
    "ramp": Role(("ramp",)),
    "draw": Role(("draw", "card-advantage")),
    "removal": Role(("removal",), minus_roles=("wipe",)),
    "wipe": Role(("sweeper",)),
    "counter": Role(("counterspell",)),
    "tutor": Role(("tutor",), exclude_tags=("tutor-land",)),
    "protection": Role(("protection",)),
    "recursion": Role(("recursion",)),
}


def compute_roles(tags: list[dict[str, Any]]) -> dict[str, set[str]]:
    by_id = {t["id"]: t for t in tags}
    by_slug = {t["slug"]: t for t in tags}

    def subtree(slugs: tuple[str, ...]) -> set[str]:
        out: set[str] = set()
        stack = [by_slug[s]["id"] for s in slugs if s in by_slug]
        for missing in (s for s in slugs if s not in by_slug):
            log.warning("La etiqueta %r ya no existe en Scryfall", missing)
        while stack:
            tag_id = stack.pop()
            if tag_id in out or tag_id not in by_id:
                continue
            out.add(tag_id)
            stack.extend(by_id[tag_id].get("child_ids") or [])
        return out

    roles: dict[str, set[str]] = {}
    for name, role in ROLES.items():
        tag_ids = subtree(role.tags) - subtree(role.exclude_tags)
        roles[name] = {
            tagging["oracle_id"]
            for tag_id in tag_ids
            for tagging in by_id[tag_id].get("taggings") or []
            if tagging.get("oracle_id")
        }
    for name, role in ROLES.items():
        for other in role.minus_roles:
            roles[name] -= roles[other]
    return roles


async def sync(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    manifest = await bulk_data.fetch_manifest(KIND)
    remote = manifest.get("updated_at", "")
    state = await db.get(BulkSyncState, KIND)
    if not force and state and state.updated_at == remote:
        return {"skipped": True}

    async with httpx.AsyncClient(
        headers={"User-Agent": SCRYFALL_USER_AGENT},
        timeout=httpx.Timeout(60.0, read=300.0),
        follow_redirects=True,
        verify=not ssl_insecure(),
    ) as client:
        resp = await client.get(manifest["jsonl_download_uri"])
        resp.raise_for_status()
    lines = gzip.decompress(resp.content).decode("utf-8").splitlines()
    roles = compute_roles([json.loads(line) for line in lines if line.strip()])

    await db.execute(delete(OracleRole))
    rows = [{"oracle_id": o, "role": r} for r, oracles in roles.items() for o in oracles]
    if rows:
        await db.execute(insert(OracleRole), rows)
    if state is None:
        state = BulkSyncState(kind=KIND)
        db.add(state)
    state.updated_at = remote
    state.synced_at = datetime.now(UTC)
    state.rows_imported = len(rows)
    state.bytes_downloaded = len(resp.content)
    await db.commit()
    log.info("Funciones de carta importadas: %s", {r: len(o) for r, o in roles.items()})
    return {"skipped": False, "roles": {r: len(o) for r, o in roles.items()}}


async def roles_for(db: AsyncSession, oracle_ids: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for chunk in chunked(list(oracle_ids)):
        rows = await db.execute(
            select(OracleRole.oracle_id, OracleRole.role).where(OracleRole.oracle_id.in_(chunk))
        )
        for oracle_id, role in rows:
            out.setdefault(oracle_id, []).append(role)
    return out
