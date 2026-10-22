from __future__ import annotations

import dataclasses
import hashlib
import io
import re
import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from mpc_forge.routes.export import _shared
from mpc_forge.routes.export import press as export_press
from mpc_forge.services.printing import press_export
from mpc_forge.services.printing.press_color import CmykConverter
from mpc_forge.services.printing.press_export import (
    PressBuildResult,
    PressExportError,
    build_press_package,
    build_press_pdf,
    build_press_slots,
    preflight,
)
from mpc_forge.services.printing.press_image import (
    WARN_CMYK_UNTAGGED,
    WARN_LOW_RESOLUTION,
    WARN_ROTATED,
    WARN_UNUSUAL_RATIO,
    PressImageError,
    mirror_pad,
    native_bleed_mm,
    prepare_press_image,
)
from mpc_forge.services.printing.print_shops import PINBRO_POKER, get_print_shop
from mpc_forge.services.printing.xml_generator import DeckCardResolved

SPEC = PINBRO_POKER
BORDER = (12, 12, 12)
ART = (210, 30, 30)
MM_PT = 72 / 25.4


def _card_png(
    path: Path,
    size: tuple[int, int] = (745, 1040),
    *,
    border: tuple[int, int, int] = BORDER,
    art: tuple[int, int, int] = ART,
    rounded: bool = True,
    bleed_px: int = 0,
) -> Path:
    """Carta sintética: borde liso, "arte" de un color y, como las PNG de
    Scryfall, esquinas transparentes."""
    w, h = size
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=round(w * 0.05) if rounded else 0, fill=(*border, 255)
    )
    inset = round(min(w, h) * 0.045) + bleed_px
    draw.rectangle((inset, inset, w - inset, h - inset), fill=(*art, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def _converter() -> CmykConverter:
    return CmykConverter(SPEC.icc_path)


def _cmyk(jpeg: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG" and img.mode == "CMYK"
    return img


def _card(name: str, front: Path, qty: int = 1, back: Path | None = None) -> DeckCardResolved:
    return DeckCardResolved(
        name=name,
        quantity=qty,
        scryfall_id=f"sf-{name}",
        front_path=front,
        back_path=back,
        back_name=f"{name} back" if back else None,
    )


class TestSpec:
    def test_pinbro_template_geometry(self):
        assert (SPEC.page_w_mm, SPEC.page_h_mm) == (69.0, 94.0)
        assert (SPEC.trim_w_mm, SPEC.trim_h_mm, SPEC.bleed_mm) == (63.0, 88.0, 3.0)
        assert SPEC.trim_w_mm - 2 * SPEC.safe_margin_mm == 57.0
        assert SPEC.page_px == (815, 1110)
        assert SPEC.package_entry == "Cartas/Póquer(63x88).pdf"
        assert get_print_shop("pinbro_poker") is SPEC and get_print_shop("nope") is None

    def test_bundled_profile_is_the_unmodified_fogra39_one(self):
        data = SPEC.icc_path.read_bytes()
        digest = "6df43849a84d2632e7900a125f657bd09364ed56775bc4740c1e39a5492d47e3"
        assert hashlib.sha256(data).hexdigest() == digest
        assert digest in (SPEC.icc_path.parent / "NOTICE.txt").read_text(encoding="utf-8")

    def test_delivery_message_is_in_the_shop_language(self):
        message = SPEC.delivery_message("Atraxa {x}", 100, 200)
        assert "«Atraxa {x}»" in message and "100 cartas" in message
        assert "200 páginas" in message and SPEC.package_entry in message


class TestBleedDetection:
    def test_known_sources(self):
        assert native_bleed_mm(745, 1040) == 0.0
        assert native_bleed_mm(822, 1122) == pytest.approx(3.05, abs=0.02)
        assert native_bleed_mm(1040, 745) is None
        assert native_bleed_mm(500, 1000) is None
        assert native_bleed_mm(0, 10) is None

    def test_mirror_pad_reflects_every_side(self):
        src = Image.new("L", (10, 6))
        src.putdata([x * 10 + y for y in range(6) for x in range(10)])
        out = mirror_pad(src, 3, 2)
        assert out.size == (16, 10)
        for k in range(3):
            assert out.getpixel((2 - k, 5)) == src.getpixel((k, 3))
            assert out.getpixel((13 + k, 5)) == src.getpixel((9 - k, 3))
        for k in range(2):
            assert out.getpixel((7, 1 - k)) == src.getpixel((4, k))
            assert out.getpixel((7, 8 + k)) == src.getpixel((4, 5 - k))
        assert mirror_pad(src, 0, 0) is src


class TestPrepareImage:
    def test_scryfall_card_gets_border_colored_bleed_and_corners(self, tmp_path):
        prepared = prepare_press_image(_card_png(tmp_path / "s.png"), SPEC, _converter())
        img = _cmyk(prepared.jpeg)
        assert img.size == SPEC.page_px and prepared.generated_bleed
        assert prepared.source_ppi == 300 and prepared.warnings == []
        border = img.getpixel((400, 40))
        # Esquina (antes transparente) y filo exterior del sangrado: el borde
        # sigue liso, sin el blanco del fondo ni el rojo del arte reflejado.
        for xy in ((2, 2), (812, 1107), (400, 1), (1, 555), (60, 60)):
            assert all(abs(a - b) <= 6 for a, b in zip(img.getpixel(xy), border, strict=True)), xy
        assert border[3] > 240
        red = img.getpixel((400, 555))
        assert red[1] > 200 and red[3] < 30

    def test_borderless_card_is_mirrored_not_smeared(self, tmp_path):
        src = Image.new("RGB", (745, 1040))
        src.putdata([((x * 7) % 256, (y * 3) % 256, 90) for y in range(1040) for x in range(745)])
        src.save(tmp_path / "b.png")
        prepared = prepare_press_image(tmp_path / "b.png", SPEC, _converter())
        img = _cmyk(prepared.jpeg)
        column = [img.getpixel((x, 555))[0] for x in range(0, 30)]
        assert len(set(column)) > 5

    def test_mpc_image_uses_its_own_bleed(self, tmp_path):
        path = _card_png(tmp_path / "m.png", (822, 1122), rounded=False, bleed_px=36)
        prepared = prepare_press_image(path, SPEC, _converter())
        assert not prepared.generated_bleed and prepared.native_bleed_mm == pytest.approx(
            3.05, 0.02
        )
        assert prepared.source_ppi == 302 and prepared.warnings == []

    def test_warnings(self, tmp_path):
        rotated = prepare_press_image(
            _card_png(tmp_path / "l.png", (1040, 745)), SPEC, _converter()
        )
        assert WARN_ROTATED in rotated.warnings and _cmyk(rotated.jpeg).size == SPEC.page_px
        low = prepare_press_image(_card_png(tmp_path / "low.png", (300, 419)), SPEC, _converter())
        assert low.warnings == [WARN_LOW_RESOLUTION]
        odd = prepare_press_image(_card_png(tmp_path / "o.png", (600, 1000)), SPEC, _converter())
        assert WARN_UNUSUAL_RATIO in odd.warnings

    def test_untagged_cmyk_is_kept_as_separated(self, tmp_path):
        Image.new("CMYK", (745, 1040), (40, 120, 200, 10)).save(tmp_path / "c.jpg", quality=95)
        prepared = prepare_press_image(tmp_path / "c.jpg", SPEC, _converter())
        assert prepared.warnings == [WARN_CMYK_UNTAGGED]
        pixel = _cmyk(prepared.jpeg).getpixel((400, 555))
        assert all(abs(a - b) <= 4 for a, b in zip(pixel, (40, 120, 200, 10), strict=True))

    def test_unreadable_files_are_errors_not_placeholders(self, tmp_path):
        with pytest.raises(PressImageError):
            prepare_press_image(tmp_path / "missing.png", SPEC, _converter())
        (tmp_path / "bad.png").write_bytes(b"not an image")
        with pytest.raises(PressImageError):
            prepare_press_image(tmp_path / "bad.png", SPEC, _converter())


class TestColor:
    def test_separation_stays_within_the_profile_ink_limit(self):
        colors = [
            (r, g, b) for r in (0, 60, 128, 255) for g in (0, 60, 128, 255) for b in (0, 128, 255)
        ]
        src = Image.new("RGB", (len(colors), 1))
        src.putdata(colors)
        out = _converter().to_cmyk(src)
        assert out.mode == "CMYK"
        inks = [sum(out.getpixel((i, 0))) for i in range(len(colors))]
        assert max(inks) <= 765 + 3
        assert out.getpixel((colors.index((255, 255, 255)), 0)) == (0, 0, 0, 0)
        assert out.getpixel((colors.index((0, 0, 0)), 0))[3] >= 253

    def test_rejects_non_cmyk_profile(self, tmp_path):
        from mpc_forge.services.printing.press_color import PressColorError

        with pytest.raises(PressColorError):
            CmykConverter(tmp_path / "missing.icc")


def _pdf_pages(pdf: bytes) -> list[tuple[bytes, bytes]]:
    """(diccionario, contenido descomprimido) de cada página, en orden."""
    objects = {
        int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*(.*?)endobj", pdf, re.S)
    }
    kids = re.search(rb"/Kids \[([^\]]*)\]", pdf)
    assert kids is not None
    pages = []
    for ref in re.findall(rb"(\d+) 0 R", kids.group(1)):
        page = objects[int(ref)]
        content_ref = re.search(rb"/Contents (\d+) 0 R", page)
        assert content_ref is not None
        stream = re.search(rb"stream\r?\n(.*)endstream", objects[int(content_ref.group(1))], re.S)
        assert stream is not None
        pages.append((page, zlib.decompress(stream.group(1).rstrip(b"\r\n"))))
    return pages


class TestPressPdf:
    @pytest.fixture
    def images(self, tmp_path) -> dict[str, Path]:
        return {
            "a": _card_png(tmp_path / "a.png", art=(30, 200, 30)),
            "b": _card_png(tmp_path / "b.png", art=(30, 30, 200)),
            "b_back": _card_png(tmp_path / "bb.png", art=(200, 200, 30)),
            "cardback": _card_png(tmp_path / "cb.png", border=(60, 40, 20), art=(90, 60, 30)),
        }

    def _build(self, images, tmp_path):
        cards = [
            _card("A", images["a"], qty=2),
            _card("B // C", images["b"], back=images["b_back"]),
        ]
        pairs = build_press_slots(cards, images["cardback"])
        ticks: list[str] = []
        result = build_press_pdf(
            pairs, tmp_path / "out" / "deck.pdf", SPEC, title="Test", on_image=ticks.append
        )
        return pairs, result, ticks

    def test_one_card_per_page_front_then_back(self, images, tmp_path):
        pairs, result, ticks = self._build(images, tmp_path)
        assert [(f.face_name, b.face_name) for f, b in pairs] == [
            ("A", "Cardback"),
            ("A", "Cardback"),
            ("B", "B // C back"),
        ]
        assert (result.total_cards, result.total_pages, result.unique_images) == (3, 6, 4)
        assert (result.cardback_backs, result.dfc_backs, result.generated_bleed_images) == (2, 1, 4)
        assert sorted(ticks) == sorted(["A", "Cardback", "B", "B // C back"])

        pages = _pdf_pages(result.pdf_path.read_bytes())
        assert len(pages) == 6
        draws = []
        for page, content in pages:
            assert b"/MediaBox [ 0 0 195.5906 266.4567 ]" in page
            assert b"/BleedBox [ 0 0 195.5906 266.4567 ]" in page
            assert b"/TrimBox [ 8.503937 8.503937 187.0866 257.9528 ]" in page
            # Una sola imagen que cubre la página entera y nada más: ni texto
            # ni trazos (marcas de corte).
            (draw,) = re.findall(rb"/(FormXob\.\w+) Do", content)
            assert b"195.5906 0 0 266.4567 0 0 cm" in content
            assert not re.search(rb"\b(BT|Tf|re|l|S)\b", content), content
            draws.append(draw)
        assert draws[0] == draws[2] and draws[1] == draws[3]
        assert len({draws[0], draws[1], draws[4], draws[5]}) == 4

    def test_pdf_is_what_a_prepress_check_expects(self, images, tmp_path):
        _, result, _ = self._build(images, tmp_path)
        pdf = result.pdf_path.read_bytes()
        assert b"/BaseFont" not in pdf and b"/FontFile" not in pdf
        assert b"DeviceRGB" not in pdf and b"/SMask" not in pdf and b"/Mask" not in pdf
        images_dicts = re.findall(rb"<<[^<>]*/Subtype /Image[^<>]*>>", pdf)
        assert len(images_dicts) == 4
        for d in images_dicts:
            assert b"/ColorSpace /DeviceCMYK" in d and b"/Filter [ /DCTDecode ]" in d
            assert b"/Width 815" in d and b"/Height 1110" in d
        intent = re.search(rb"/OutputIntents \[ <<(.*?)>> \]", pdf, re.S)
        assert intent is not None
        assert b"/S /GTS_PDFX" in intent.group(1)
        assert b"/OutputConditionIdentifier (FOGRA39)" in intent.group(1)
        assert b"/RegistryName (http://www.color.org)" in intent.group(1)
        assert b"/Trapped /False" in pdf
        assert result.pdf_size_bytes <= SPEC.max_bytes_per_page * result.total_pages
        assert result.largest_image_bytes < SPEC.max_bytes_per_page

    def test_package_layout(self, images, tmp_path):
        _, result, _ = self._build(images, tmp_path)
        size = build_press_package(result.pdf_path, tmp_path / "pkg.zip", SPEC)
        with zipfile.ZipFile(tmp_path / "pkg.zip") as zf:
            (info,) = zf.infolist()
            assert info.filename == "Cartas/Póquer(63x88).pdf"
            assert info.flag_bits & 0x800
            assert zf.read(info) == result.pdf_path.read_bytes()
        assert size == (tmp_path / "pkg.zip").stat().st_size

    def test_cardback_is_required_unless_every_card_has_its_own_back(self, images):
        with pytest.raises(PressExportError) as err:
            build_press_slots(
                [_card("A", images["a"]), _card("B", images["b"], back=images["a"])], None
            )
        assert err.value.code == "missing_cardback" and err.value.cards == ["A"]
        only_dfc = build_press_slots([_card("B", images["b"], back=images["b_back"])], None)
        assert [b.source for _, b in only_dfc] == ["dfc"]

    def test_preflight_rejects_what_the_shop_would(self, tmp_path):
        ok = PressBuildResult(
            pdf_path=tmp_path / "x.pdf",
            total_cards=2,
            total_pages=4,
            unique_images=2,
            dfc_backs=0,
            cardback_backs=2,
            generated_bleed_images=0,
            pdf_size_bytes=4 * SPEC.max_bytes_per_page,
            largest_image_bytes=SPEC.max_bytes_per_page,
            min_source_ppi=300,
        )
        preflight(ok, SPEC)
        for change, code in (
            ({"total_pages": 3}, "page_count"),
            ({"pdf_size_bytes": 4 * SPEC.max_bytes_per_page + 1}, "file_too_large"),
            ({"largest_image_bytes": SPEC.max_bytes_per_page + 1}, "page_too_large"),
        ):
            with pytest.raises(PressExportError) as err:
                preflight(dataclasses.replace(ok, **change), SPEC)
            assert err.value.code == code

    def test_image_errors_stop_the_build(self, images, tmp_path):
        pairs = build_press_slots([_card("A", tmp_path / "missing.png")], images["cardback"])
        with pytest.raises(PressImageError):
            build_press_pdf(pairs, tmp_path / "x.pdf", SPEC, title="x")
        with pytest.raises(PressExportError):
            build_press_pdf([], tmp_path / "x.pdf", SPEC, title="x")


class TestEndpoint:
    @pytest.fixture
    def resolved(self, tmp_path, monkeypatch):
        """Resuelve el mazo de prueba con imágenes reales en disco y un dorso."""
        cardback = _card_png(tmp_path / "back.png", border=(20, 20, 60), art=(40, 40, 160))
        state = {"drop": None}

        async def fake_resolve(db, scryfall, art_cache, deck_obj, on_progress=None):
            from sqlalchemy import select

            from mpc_forge.models import DeckCard

            cards = (
                await db.scalars(
                    select(DeckCard)
                    .where(DeckCard.deck_id == deck_obj.id)
                    .order_by(DeckCard.role, DeckCard.name)
                )
            ).all()
            return [
                _card(c.name, _card_png(tmp_path / f"{c.id}.png"), qty=c.quantity)
                for c in cards
                if c.name != state["drop"]
            ]

        async def fake_cardback(db, deck_obj):
            return cardback

        monkeypatch.setattr(_shared, "resolve_deck_for_xml", fake_resolve)
        monkeypatch.setattr(export_press, "resolve_deck_cardback", fake_cardback)
        return state

    async def test_lists_the_shops(self, client):
        shops = (await client.get("/api/print-shops")).json()
        assert [s["key"] for s in shops] == ["pinbro_poker"]
        assert (
            shops[0]["page_w_mm"] == 69.0 and shops[0]["delivery_email"] == "info@pinbrogames.com"
        )

    async def test_exports_the_package(self, client, deck, resolved):
        r = await client.post(
            f"/api/decks/{deck['id']}/export-press", json={"shop": "pinbro_poker"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["total_cards"], body["total_pages"], body["cardback_backs"]) == (3, 6, 3)
        assert body["warnings"] == [] and "3 cartas" in body["delivery_message"]
        assert body["zip_filename"].startswith("pinbro-poker-test-deck-")
        zip_bytes = (await client.get(f"/api/exports/{body['zip_filename']}")).content
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == ["Cartas/Póquer(63x88).pdf"]
        pdf = await client.get(f"/api/exports/{body['pdf_filename']}")
        assert pdf.headers["content-type"] == "application/pdf"

        progress = (await client.get(f"/api/decks/{deck['id']}/build-progress")).json()
        assert progress["kind"] == "press" and progress["done"] and progress["error"] is None
        activity = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        events = activity["events"] if isinstance(activity, dict) else activity
        assert any(e["kind"] == "press_exported" for e in events)

    async def test_refuses_an_incomplete_deck(self, client, deck, resolved):
        resolved["drop"] = "Sol Ring"
        r = await client.post(f"/api/decks/{deck['id']}/export-press", json={})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "missing_images"
        assert r.json()["detail"]["cards"] == ["Sol Ring"]
        progress = (await client.get(f"/api/decks/{deck['id']}/build-progress")).json()
        assert progress["done"] and progress["error"]

    async def test_refuses_a_deck_without_cardback(self, client, deck, resolved, monkeypatch):
        async def no_cardback(db, deck_obj):
            return None

        monkeypatch.setattr(export_press, "resolve_deck_cardback", no_cardback)
        r = await client.post(f"/api/decks/{deck['id']}/export-press", json={})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "missing_cardback"

    async def test_unknown_shop(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/export-press", json={"shop": "x"})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_shop"


def test_workers_are_bounded():
    assert 1 <= press_export._MAX_WORKERS <= 8
