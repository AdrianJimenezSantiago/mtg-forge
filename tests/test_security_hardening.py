"""Tests de las defensas añadidas para el modelo de amenaza local.

La app abre un puerto HTTP sin autenticación en la máquina del usuario. Eso la
protege de la red, pero no del navegador que el propio usuario tiene abierto:
una web maliciosa puede intentar hablar con 127.0.0.1. Estas pruebas cubren las
tres defensas contra eso y la no-filtración de credenciales.
"""
from __future__ import annotations

import logging

import pytest

from mpc_forge.middleware import _hostname, is_same_origin
from mpc_forge.services import settings as settings_service
from mpc_forge.services.logging_setup import RedactSecretsFilter, redact

FAKE_KEY = "AIzaSyD-ExampleKeyForTestsOnly-0123456789"


# ---------------------------------------------------------------------------
# Cabecera Host / DNS rebinding
# ---------------------------------------------------------------------------

class TestHostValidation:
    async def test_localhost_is_accepted(self, client):
        assert (await client.get("/api/settings/")).status_code == 200

    @pytest.mark.parametrize("host", [
        "evil.com",
        "attacker.example",
        # El caso real de DNS rebinding: un dominio que resuelve a 127.0.0.1
        # pero llega con su propio nombre en la cabecera Host.
        "rebind.attacker-dns.example",
        "127.0.0.1.evil.com",
    ])
    async def test_foreign_host_is_rejected(self, client, host):
        r = await client.get("/api/settings/", headers={"host": host})
        assert r.status_code == 400
        # Y, sobre todo, no ha devuelto los ajustes.
        assert "definitions" not in r.text

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:9999", "[::1]:8765",
    ])
    async def test_local_hosts_with_any_port(self, client, host):
        assert (await client.get("/api/settings/", headers={"host": host})).status_code == 200

    def test_hostname_parsing_handles_ipv6(self):
        """Partir por el primer ':' rompería en IPv6, que lleva varios."""
        assert _hostname("[::1]:8765") == "[::1]"
        assert _hostname("127.0.0.1:8765") == "127.0.0.1"
        assert _hostname("LocalHost") == "localhost"


class TestCrossSiteWrites:
    async def test_same_origin_write_is_allowed(self, client, deck):
        r = await client.patch(
            f"/api/decks/{deck['id']}",
            json={"name": "Renombrado"},
            headers={"sec-fetch-site": "same-origin"},
        )
        assert r.status_code == 200

    async def test_cross_site_write_is_rejected(self, client, deck):
        r = await client.patch(
            f"/api/decks/{deck['id']}",
            json={"name": "Secuestrado"},
            headers={"sec-fetch-site": "cross-site"},
        )
        assert r.status_code == 403

    async def test_cross_site_read_is_still_allowed(self, client, deck):
        """Solo se bloquean las escrituras.

        Un GET cross-site no puede leerse por la política de mismo origen del
        navegador, así que bloquearlo aquí no añadiría seguridad y sí rompería
        casos legítimos como abrir una imagen en una pestaña.
        """
        r = await client.get(
            f"/api/decks/{deck['id']}", headers={"sec-fetch-site": "cross-site"}
        )
        assert r.status_code == 200


class TestOpenRedirect:
    async def test_referer_to_another_site_is_not_followed(self, client):
        r = await client.post(
            "/set-lang",
            data={"lang": "en"},
            headers={"referer": "https://evil.example/phishing"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/"

    async def test_local_referer_is_preserved(self, client):
        r = await client.post(
            "/set-lang",
            data={"lang": "en"},
            headers={"referer": "http://127.0.0.1:8765/collection"},
            follow_redirects=False,
        )
        assert r.headers["location"].endswith("/collection")

    def test_protocol_relative_url_is_not_same_origin(self):
        """``//evil.com`` parece una ruta pero es una URL absoluta."""
        class _Req:
            class url:
                netloc = "127.0.0.1:8765"
        assert is_same_origin(_Req(), "/decks/1") is True
        assert is_same_origin(_Req(), "//evil.com/x") is False
        assert is_same_origin(_Req(), "https://evil.com/x") is False


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------

class TestSecretsAreNotExposed:
    async def test_api_key_is_not_returned_after_saving(self, client):
        saved = await client.put(
            "/api/settings/", json={"values": {"google_api_key": FAKE_KEY}}
        )
        assert saved.status_code == 200
        assert FAKE_KEY not in saved.text

        read = await client.get("/api/settings/")
        assert FAKE_KEY not in read.text
        body = read.json()
        assert body["values"]["google_api_key"] == ""
        # Pero la UI sí sabe que hay una guardada.
        assert "google_api_key" in body["secrets_set"]

    async def test_definitions_mark_the_key_as_secret(self, client):
        defs = (await client.get("/api/settings/")).json()["definitions"]
        by_key = {d["key"]: d for d in defs}
        assert by_key["google_api_key"]["secret"] is True
        assert by_key["ssl_insecure"]["secret"] is False

    async def test_empty_string_does_not_wipe_the_key(self, client, db_session=None):
        """Guardar el formulario completo no debe borrar la credencial.

        La UI recibe "" al leer (el backend no devuelve el valor), así que si
        "" significara "borrar", cualquier PUT del formulario entero se la
        llevaría por delante.
        """
        await client.put("/api/settings/", json={"values": {"google_api_key": FAKE_KEY}})
        await client.put("/api/settings/", json={"values": {"google_api_key": ""}})
        body = (await client.get("/api/settings/")).json()
        assert "google_api_key" in body["secrets_set"]

    async def test_sentinel_clears_the_key(self, client):
        await client.put("/api/settings/", json={"values": {"google_api_key": FAKE_KEY}})
        await client.put(
            "/api/settings/",
            json={"values": {"google_api_key": settings_service.SECRET_CLEAR}},
        )
        body = (await client.get("/api/settings/")).json()
        assert "google_api_key" not in body["secrets_set"]


class TestLogRedaction:
    @pytest.mark.parametrize("text", [
        f"GET https://www.googleapis.com/drive/v3/files?q=x&key={FAKE_KEY}",
        f"Authorization: Bearer {FAKE_KEY}",
        f"clave suelta {FAKE_KEY} en medio del texto",
        "https://api.example/x?access_token=abc123def&other=1",
    ])
    def test_credentials_are_redacted(self, text):
        cleaned = redact(text)
        assert FAKE_KEY not in cleaned
        assert "abc123def" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_harmless_text_is_untouched(self):
        text = "Indexado completo: 1234 ficheros en 5 carpetas"
        assert redact(text) == text

    def test_filter_redacts_interpolated_args(self):
        """El secreto suele llegar por los args, no por el literal del mensaje."""
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="Fallo pidiendo %s", args=(f"https://x/y?key={FAKE_KEY}",),
            exc_info=None,
        )
        RedactSecretsFilter().filter(record)
        assert FAKE_KEY not in record.getMessage()

    def test_filter_redacts_exception_text(self):
        """httpx mete la URL completa en el str() de sus excepciones."""
        try:
            raise ValueError(f"peticion a https://x/y?key={FAKE_KEY} fallida")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="fallo", args=(), exc_info=sys.exc_info(),
            )
        RedactSecretsFilter().filter(record)
        assert FAKE_KEY not in (record.exc_text or "")
