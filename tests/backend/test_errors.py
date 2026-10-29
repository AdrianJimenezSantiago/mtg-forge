from __future__ import annotations

import ast
import re
from pathlib import Path

from mpc_forge import errors
from mpc_forge.services.system.i18n import _TRANSLATIONS, BASE_LANG
from mpc_forge.services.system.settings import DEFINITIONS, definitions_dump

PACKAGE = Path(__file__).resolve().parents[2] / "mpc_forge"


def _codes_in_source() -> set[str]:
    codes: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "ApiError" and len(node.args) >= 2:
                arg = node.args[1]
            elif name == "from_exception" and len(node.args) >= 3:
                arg = node.args[2]
            else:
                arg = next((k.value for k in node.keywords if k.arg == "code"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                codes.add(arg.value)
        for match in re.finditer(r'return False, "([a-z_]+)"', path.read_text(encoding="utf-8")):
            codes.add(match.group(1))
    return codes


class TestErrorCatalogue:
    def test_every_error_code_raised_has_a_translation(self):
        codes = _codes_in_source()
        assert len(codes) > 40
        for lang, table in _TRANSLATIONS.items():
            missing = sorted(c for c in codes if f"error_{c}" not in table)
            assert not missing, f"Errores sin traducir en '{lang}': {missing}"

    def test_messages_are_formatted_with_their_params(self):
        assert errors.translate_message("en", "unknown_format", {"format": "dek"}) == (
            "Unknown format: dek"
        )
        assert errors.translate_message("en", "unknown_format", {}) == "Unknown format: {format}"
        assert errors.translate_message("en", "no_such_code") is None

    def test_detail_falls_back_to_the_message_or_reason(self):
        assert errors.localize_detail("plain", "en") == "plain"
        assert errors.localize_detail({"code": "nope", "message": "m"}, "en")["message"] == "m"
        detail = errors.localize_detail({"code": "nope", "params": {"reason": "r"}}, "en")
        assert detail["message"] == "r"
        assert errors.localize_detail({"code": "nope"}, "en")["message"] == "nope"

    def test_coded_exceptions_keep_their_params(self):
        class Boom(errors.CodedError):
            code = "boom"

        exc = errors.from_exception(400, Boom(code="undo_card_gone", card="x"), "fallback")
        assert exc.detail == {"code": "undo_card_gone", "params": {"card": "x"}}
        plain = errors.from_exception(400, ValueError("nope"), "invalid_setting")
        assert plain.detail == {"code": "invalid_setting", "params": {"reason": "nope"}}


class TestLocalisedResponses:
    async def test_errors_follow_the_language_cookie(self, client):
        client.cookies.set("lang", "en")
        r = await client.get("/api/decks/999999")
        assert r.status_code == 404
        assert r.json()["detail"] == {
            "code": "deck_not_found",
            "params": {},
            "message": "Deck not found.",
        }
        client.cookies.set("lang", "es")
        r = await client.get("/api/decks/999999")
        assert r.json()["detail"]["message"] == "Mazo no encontrado."

    async def test_import_errors_name_the_url(self, client):
        client.cookies.set("lang", "en")
        r = await client.post("/api/decks/import/url", json={"url": "https://example.org/x"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "unsupported_url"
        assert "https://example.org/x" in detail["message"]

    async def test_settings_definitions_are_translated(self, client):
        client.cookies.set("lang", "en")
        defs = {d["key"]: d for d in (await client.get("/api/settings/")).json()["definitions"]}
        assert defs["auto_update"]["label"] == "Update on startup"
        assert defs["paths.backups_dir"]["section"] == "backup"
        es = {d["key"]: d for d in definitions_dump("es")}
        assert es["auto_update"]["label"] == "Actualizar al arrancar"


def test_every_setting_has_both_translations():
    base = _TRANSLATIONS[BASE_LANG]
    for sd in DEFINITIONS:
        key = sd.key.replace(".", "_")
        assert f"setting_{key}_label" in base and f"setting_{key}_desc" in base
