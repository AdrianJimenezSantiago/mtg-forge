from __future__ import annotations

import dataclasses
import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import CustomArt, DeckCard, PrintingCache
from mpc_forge.services.art import custom_art, thumbnails
from mpc_forge.services.art.name_matching import card_keys, match_key

needs_pillow = pytest.mark.skipif(
    not thumbnails.pillow_available(), reason="Pillow no está instalado"
)


def _png(size=(745, 1040), color=(30, 60, 120), fmt="PNG") -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def _write(path: Path, color=(30, 60, 120)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png(color=color, fmt="JPEG" if path.suffix == ".jpg" else "PNG"))
    return path


@pytest.fixture
def custom_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "custom"
    root.mkdir()
    monkeypatch.setattr(cfg, "PATHS", dataclasses.replace(cfg.PATHS, custom_art_dir=root))
    custom_art.set_linked_roots({})
    return root


def _card(deck: dict, name: str) -> dict:
    return next(c for c in deck["cards"] if c["name"] == name)


async def _add_dfc(deck_id: int) -> int:
    async with session_scope() as db:
        db.add(
            PrintingCache(
                scryfall_id="delver-en",
                oracle_id="oracle-delver",
                name="Delver of Secrets // Insectile Aberration",
                back_name="Insectile Aberration",
                set_code="isd",
                set_name="Innistrad",
                collector_number="51",
                rarity="common",
                layout="transform",
            )
        )
        dc = DeckCard(
            deck_id=deck_id,
            oracle_id="oracle-delver",
            name="Delver of Secrets // Insectile Aberration",
            quantity=1,
            scryfall_id="delver-en",
            role="mainboard",
        )
        db.add(dc)
        await db.commit()
        return dc.id


async def _plan(client, deck_id: int, **body) -> dict:
    r = await client.post(f"/api/decks/{deck_id}/custom-art/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _proposed(plan: dict) -> list[dict]:
    return [
        {"deck_card_id": i["deck_card_id"], "face": i["face"], "custom_art_id": i["proposed"]}
        for i in plan["items"]
        if i["status"] == "match"
    ]


class TestNameMatching:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Lim-Dûl's Vault", "lim duls vault"),
            ("Æther Vial", "Aether Vial"),
            ("Circle of Protection: Red", "Circle of Protection- Red"),
            ("Sol Ring", "sol_ring"),
            ("Jötun Grunt", "JOTUN GRUNT"),
            ("Urza’s Saga", "Urza's Saga"),
        ],
    )
    def test_equivalent_spellings_share_a_key(self, a, b):
        assert match_key(a) == match_key(b)

    def test_key_never_empty(self):
        assert match_key("_____") == "_____"

    def test_double_faced_cards_expose_each_face(self):
        keys = card_keys("Delver of Secrets // Insectile Aberration")
        assert "delver of secrets" in keys.front
        assert "delver of secrets insectile aberration" in keys.front
        assert keys.back == {"insectile aberration"}

    def test_trailing_parenthesis_is_optional(self):
        assert "b f m" in card_keys("B.F.M. (Big Furry Monster)").front

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Sol Ring.png", ("sol ring", "front", None)),
            ("sol_ring.png", ("sol ring", "front", None)),
            ("Sol Ring - Anime.png", ("sol ring", "front", "Anime")),
            ("Sol Ring (Jane Doe).png", ("sol ring", "front", "Jane Doe")),
            ("Sol Ring [SLD].png", ("sol ring", "front", "SLD")),
            ("001 Sol Ring.png", ("sol ring", "front", None)),
            ("12. Sol Ring.png", ("sol ring", "front", None)),
            ("03 - Sol Ring.jpg", ("sol ring", "front", None)),
            ("1996 World Champion.png", ("1996 world champion", "front", None)),
            ("Delver of Secrets [BACK].png", ("delver of secrets", "back", None)),
            ("Fire -- Ice.png", ("fire ice", "front", None)),
        ],
    )
    def test_parse_filename(self, filename, expected):
        assert custom_art.parse_filename(Path(filename)) == expected


@needs_pillow
class TestLibraryPlanAndApply:
    async def test_whole_deck_round_trip_with_undo(self, client, deck, custom_dir):
        _write(custom_dir / "Proxies" / "Sol Ring.png")
        _write(custom_dir / "Proxies" / "command_tower (Anime).jpg")
        _write(custom_dir / "Proxies" / "001 - Arcane Signet.png")
        _write(custom_dir / "Proxies" / "Llanowar Elves.png")
        deck_id = deck["id"]

        plan = await _plan(client, deck_id, rescan=True)
        assert plan["summary"]["match"] == 3 and plan["summary"]["missing"] == 0
        assert plan["unused"] == []

        r = await client.post(
            f"/api/decks/{deck_id}/custom-art/apply", json={"assignments": _proposed(plan)}
        )
        result = r.json()
        assert r.status_code == 200 and result["changed"] == 3 and result["snapshot_id"]

        view = (await client.get(f"/api/decks/{deck_id}")).json()
        assert all(c["custom_art_front_id"] for c in view["cards"])

        again = await _plan(client, deck_id)
        assert again["summary"]["same"] == 3 and _proposed(again) == []

        activity = (await client.get(f"/api/decks/{deck_id}/activity")).json()
        event = next(e for e in activity if e["kind"] == "custom_art_applied")
        assert event["payload"]["count"] == 3 and event["id"] == result["event_id"]

        r = await client.post(f"/api/decks/{deck_id}/activity/{event['id']}/undo")
        assert r.status_code == 200, r.text
        view = (await client.get(f"/api/decks/{deck_id}")).json()
        assert not any(c["custom_art_front_id"] for c in view["cards"])

    async def test_keeps_existing_custom_art_unless_overwriting(self, client, deck, custom_dir):
        _write(custom_dir / "Sol Ring - A.png", color=(1, 1, 1))
        _write(custom_dir / "Sol Ring - B.png", color=(2, 2, 2))
        await client.post("/api/custom-art/rescan")
        arts = (await client.get("/api/custom-art/")).json()
        b = next(a for a in arts if a["variant_label"] == "B")
        sol = _card(deck, "Sol Ring")
        await client.post(
            f"/api/decks/{deck['id']}/cards/change-art",
            json={"deck_card_id": sol["id"], "custom_art_id": b["id"], "face": "front"},
        )

        item = next(
            i for i in (await _plan(client, deck["id"]))["items"] if i["name"] == "Sol Ring"
        )
        assert item["status"] == "same" and len(item["candidates"]) == 2

        await client.post(
            f"/api/decks/{deck['id']}/cards/change-art",
            json={"deck_card_id": sol["id"], "scryfall_id": "sr-en", "face": "front"},
        )
        _write(custom_dir / "elsewhere" / "Other.png")
        async with session_scope() as db:
            other = CustomArt(
                filename="Other.png",
                relative_path="elsewhere/Other.png",
                card_name_normalized="other",
                face="front",
            )
            db.add(other)
            await db.flush()
            card = await db.get(DeckCard, sol["id"])
            assert card is not None
            card.custom_art_front_id = other.id
            await db.commit()

        keep = await _plan(client, deck["id"])
        item = next(i for i in keep["items"] if i["name"] == "Sol Ring")
        assert item["status"] == "keep" and item["proposed"] is None

        forced = await _plan(client, deck["id"], overwrite=True)
        item = next(i for i in forced["items"] if i["name"] == "Sol Ring")
        assert item["status"] == "match" and item["proposed"] == item["candidates"][0]["ref"]

    async def test_double_faced_card_gets_front_and_back(self, client, deck, custom_dir):
        card_id = await _add_dfc(deck["id"])
        _write(custom_dir / "Delver of Secrets.png", color=(5, 5, 5))
        _write(custom_dir / "Insectile Aberration.png", color=(6, 6, 6))

        plan = await _plan(client, deck["id"], rescan=True)
        faces = {i["face"]: i for i in plan["items"] if i["deck_card_id"] == card_id}
        assert faces["front"]["candidates"][0]["filename"] == "Delver of Secrets.png"
        assert faces["back"]["candidates"][0]["filename"] == "Insectile Aberration.png"
        assert plan["summary"]["backs"] == 1

        await client.post(
            f"/api/decks/{deck['id']}/custom-art/apply", json={"assignments": _proposed(plan)}
        )
        prints = (await client.get(f"/api/decks/{deck['id']}/cards/{card_id}/prints")).json()
        assert {c["face"] for c in prints["custom"]} == {"front", "back"}

    async def test_apply_ignores_foreign_cards_and_unknown_art(self, client, deck, custom_dir):
        _write(custom_dir / "Sol Ring.png")
        plan = await _plan(client, deck["id"], rescan=True)
        good = _proposed(plan)
        bad = [
            {"deck_card_id": 999_999, "face": "front", "custom_art_id": good[0]["custom_art_id"]},
            {"deck_card_id": good[0]["deck_card_id"], "face": "front", "custom_art_id": 999_999},
        ]
        r = await client.post(
            f"/api/decks/{deck['id']}/custom-art/apply", json={"assignments": good + bad}
        )
        assert r.json()["changed"] == 1 and r.json()["invalid"] == 2
        r = await client.post(
            f"/api/decks/{deck['id']}/custom-art/apply", json={"assignments": good}
        )
        assert r.json() | {"snapshot_id": None} == r.json() and r.json()["unchanged"] == 1
        missing = await client.post("/api/decks/999999/custom-art/plan", json={})
        assert missing.status_code == 404


class TestFilesPlan:
    async def test_matches_names_and_reports_the_rest(self, client, deck):
        names = [
            "Mazo/Sol Ring.png",
            "Mazo/Command Tower - Alt.webp",
            "Mazo/Arcane Signte.png",
            "Mazo/Totally Unrelated.png",
            "Mazo/notes.txt",
            "Mazo/.DS_Store",
        ]
        r = await client.post(
            f"/api/decks/{deck['id']}/custom-art/plan-files", json={"filenames": names}
        )
        plan = r.json()
        assert r.status_code == 200, r.text
        by_name = {i["name"]: i for i in plan["items"]}
        assert by_name["Sol Ring"]["proposed"] == 0
        assert by_name["Command Tower"]["candidates"][0]["variant"] == "Alt"
        assert by_name["Arcane Signet"]["status"] == "missing"
        unused = {u["filename"]: u for u in plan["unused"]}
        assert unused["Arcane Signte.png"]["suggestions"] == [_card(deck, "Arcane Signet")["id"]]
        assert unused["Totally Unrelated.png"]["suggestions"] == []
        assert plan["rejected"] == ["Mazo/notes.txt", "Mazo/.DS_Store"]


@needs_pillow
class TestZipImport:
    async def test_imports_images_safely_and_plans_them(self, client, deck, custom_dir):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck/Sol Ring.png", _png(color=(9, 9, 9)))
            zf.writestr("deck/sub/Command Tower.png", _png(color=(8, 8, 8)))
            zf.writestr("../../evil/Arcane Signet.png", _png(color=(7, 7, 7)))
            zf.writestr("deck/readme.txt", b"hola")
            zf.writestr("__MACOSX/deck/._Sol Ring.png", b"junk")
            zf.writestr("deck/tiny.png", _png(size=(50, 70)))
        r = await client.post(
            "/api/custom-art/import-zip",
            files={"file": ("Mi mazo.zip", buf.getvalue(), "application/zip")},
        )
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["added"] == 3 and len(body["art_ids"]) == 3
        assert {s["code"] for s in body["skipped"]} == {"unsupported_format", "too_small"}
        extracted = sorted(p.relative_to(custom_dir).as_posix() for p in custom_dir.rglob("*.png"))
        assert all(p.startswith("_imported/Mi mazo/") for p in extracted)
        assert not (custom_dir.parent / "evil").exists()

        plan = await _plan(client, deck["id"], art_ids=body["art_ids"])
        assert plan["summary"]["match"] == 3

        again = await client.post(
            "/api/custom-art/import-zip",
            files={"file": ("Mi mazo.zip", buf.getvalue(), "application/zip")},
        )
        assert again.json()["added"] == 0 and again.json()["duplicates"] == 3

    async def test_rejects_what_is_not_a_zip(self, client, custom_dir):
        r = await client.post(
            "/api/custom-art/import-zip", files={"file": ("x.zip", b"nope", "application/zip")}
        )
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_zip"


@needs_pillow
class TestLinkedFolders:
    async def test_link_serve_survive_offline_and_unlink(self, client, deck, custom_dir, tmp_path):
        linked = tmp_path / "Mis proxies"
        _write(linked / "Sol Ring.png")
        _write(linked / ".hidden" / "Command Tower.png")

        r = await client.post("/api/custom-art/folders", json={"path": str(linked)})
        folder = r.json()
        assert r.status_code == 201, r.text
        assert folder["file_count"] == 1 and folder["online"] and folder["label"] == "Mis proxies"

        arts = (await client.get("/api/custom-art/")).json()
        assert [a["relative_path"] for a in arts] == [f"_linked/{folder['id']}/Sol Ring.png"]
        served = await client.get(arts[0]["image_url"])
        assert (
            served.status_code == 200 and served.content == (linked / "Sol Ring.png").read_bytes()
        )
        thumb = await client.get(thumbnails.url_for_relative(arts[0]["relative_path"]))
        assert thumb.status_code in (200, 302)
        escape = await client.get(f"/custom_art/_linked/{folder['id']}/../../custom/x.png")
        assert escape.status_code == 404

        plan = await _plan(client, deck["id"], folder_id=folder["id"])
        await client.post(
            f"/api/decks/{deck['id']}/custom-art/apply", json={"assignments": _proposed(plan)}
        )
        folders = (await client.get("/api/custom-art/folders")).json()
        assert folders[0]["in_use"] == 1

        r = await client.delete(f"/api/custom-art/{arts[0]['id']}")
        assert r.status_code == 409 and (linked / "Sol Ring.png").exists()

        linked.rename(tmp_path / "desconectada")
        stats = (await client.post("/api/custom-art/rescan")).json()
        assert stats["offline_folders"] == 1 and stats["removed"] == 0
        assert not (await client.get("/api/custom-art/folders")).json()[0]["online"]
        (tmp_path / "desconectada").rename(linked)

        r = await client.delete(f"/api/custom-art/folders/{folder['id']}")
        assert r.status_code == 200 and r.json()["removed_images"] == 1
        assert (linked / "Sol Ring.png").exists()
        view = (await client.get(f"/api/decks/{deck['id']}")).json()
        assert _card(view, "Sol Ring")["custom_art_front_id"] is None

    async def test_rejects_folders_that_cannot_be_linked(self, client, custom_dir, tmp_path):
        cases = {
            str(tmp_path / "no-existe"): "not_a_folder",
            str(custom_dir): "inside_app_folder",
            str(custom_dir / ".."): "inside_app_folder",
            Path(tmp_path).anchor: "too_broad",
        }
        for path, code in cases.items():
            r = await client.post("/api/custom-art/folders", json={"path": path})
            assert r.status_code == 400 and r.json()["detail"]["code"] == code, (path, r.text)

        ok = tmp_path / "ok"
        ok.mkdir()
        assert (
            await client.post("/api/custom-art/folders", json={"path": str(ok)})
        ).status_code == 201
        r = await client.post("/api/custom-art/folders", json={"path": str(ok)})
        assert r.json()["detail"]["code"] == "already_linked"
        (ok / "inner").mkdir()
        r = await client.post("/api/custom-art/folders", json={"path": str(ok / "inner")})
        assert r.json()["detail"]["code"] == "overlaps"


@needs_pillow
class TestDeletingArtDetachesIt:
    async def test_deleted_art_never_resurfaces_on_a_card(self, client, deck, custom_dir):
        _write(custom_dir / "Sol Ring.png")
        plan = await _plan(client, deck["id"], rescan=True)
        await client.post(
            f"/api/decks/{deck['id']}/custom-art/apply", json={"assignments": _proposed(plan)}
        )
        (custom_dir / "Sol Ring.png").unlink()
        await client.post("/api/custom-art/rescan")
        async with session_scope() as db:
            refs = (await db.scalars(select(DeckCard.custom_art_front_id))).all()
        assert set(refs) == {None}
