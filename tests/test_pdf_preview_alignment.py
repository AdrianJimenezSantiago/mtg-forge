"""La vista previa de PDF Studio debe colocar cartas y placeholders sobre las
guías de corte, igual que el PDF real (``pdf_generator._render_page``).

Regresión: con sangrado activo, el placeholder "Cardback" (reverso sin imagen)
ocupaba el slot completo, sangrado incluido, y en las hojas de reversos parecía
desplazado respecto a las guías.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js no disponible")
def test_every_card_and_placeholder_sits_on_a_cut_guide():
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "js" / "pdf_preview_alignment.mjs"),
         str(ROOT / "static" / "js" / "pdf-studio.js")],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["cases"] == 48
    assert report["misaligned"] == [], report["misaligned"][:3]


class TestWindowsConnectionResetFilter:
    """El ruido de asyncio en Windows (WinError 10054) no debe llegar al log
    como ERROR, pero cualquier otra excepción sí."""

    def _context(self, exc, handle="<Handle _ProactorBasePipeTransport._call_connection_lost()>"):
        return {"exception": exc, "handle": handle, "message": "Exception in callback"}

    def test_detects_the_proactor_shutdown_reset(self):
        from mpc_forge.app import _is_benign_connection_reset
        assert _is_benign_connection_reset(self._context(ConnectionResetError(10054, "reset")))

    def test_other_errors_are_not_hidden(self):
        from mpc_forge.app import _is_benign_connection_reset
        assert not _is_benign_connection_reset(self._context(ValueError("boom")))
        assert not _is_benign_connection_reset(
            self._context(ConnectionResetError(), handle="<Handle something_else()>"))

    def test_handler_filters_only_the_benign_case(self, monkeypatch):
        import asyncio

        from mpc_forge import app as app_mod
        monkeypatch.setattr(app_mod.sys, "platform", "win32")
        loop = asyncio.new_event_loop()
        seen = []
        loop.set_exception_handler(lambda _l, ctx: seen.append(ctx["exception"]))
        try:
            app_mod._silence_windows_connection_resets(loop)
            loop.call_exception_handler(self._context(ConnectionResetError(10054, "reset")))
            loop.call_exception_handler(self._context(RuntimeError("real problem")))
        finally:
            loop.close()
        assert [type(e) for e in seen] == [RuntimeError]
