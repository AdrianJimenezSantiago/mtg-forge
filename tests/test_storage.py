"""Cálculo de almacenamiento local y limpieza de lo recuperable.

Qué se protege aquí
-------------------
1. **Que el total sea el total.** El desglose se pinta como "esto es lo que
   ocupa la app"; si una carpeta se queda fuera de :data:`CATEGORIES` o un
   fichero suelto de ``data_dir`` no cae en ninguna categoría, la cifra miente
   y nadie lo nota hasta que el usuario compara con el explorador de archivos.

2. **Que la limpieza no pueda tocar lo irrecuperable.** La base de datos, el
   arte custom y los reversos no se descargan de ningún sitio. Que el servicio
   rechace esos objetivos no es una comprobación de cortesía: es la única
   barrera entre un ``targets: ["database"]`` mal escrito y la pérdida de
   todos los mazos del usuario.

3. **Que las cifras se refresquen cuando el disco cambia.** El snapshot se
   cachea un minuto; sin invalidarlo al purgar, la pantalla enseñaría el
   tamaño de antes justo después de que el usuario haya borrado medio giga.
"""
from __future__ import annotations

import pytest

from mpc_forge import config as cfg
from mpc_forge.services import storage


@pytest.fixture(autouse=True)
def clean_cache():
    """Cada test arranca sin snapshot cacheado y deja el de al lado limpio."""
    storage.invalidate()
    yield
    storage.invalidate()


@pytest.fixture
def disk_content(tmp_path, monkeypatch):
    """Instalación de mentira con contenido de tamaño conocido en cada carpeta.

    Se monta en su propio ``tmp_path`` en lugar de reutilizar las rutas
    compartidas de ``conftest``: ahí el resto de la suite deja PDFs, artes y
    miniaturas, y un test que afirma "las miniaturas ocupan 150 bytes" pasaría
    o fallaría según el orden en que pytest decidiera ejecutarlo.

    Los tamaños son distintos entre sí a propósito: si el escaneo confundiera
    dos carpetas, unos bytes idénticos lo taparían.
    """
    p = cfg.PATHS.__class__(
        data_dir=tmp_path,
        db_path=tmp_path / "mpc_forge.sqlite3",
        art_dir=tmp_path / "art",
        custom_art_dir=tmp_path / "custom_art",
        exports_dir=tmp_path / "exports",
        backups_dir=tmp_path / "backups",
        cardbacks_dir=tmp_path / "cardbacks",
        thumbs_dir=tmp_path / "thumbs",
    )
    monkeypatch.setattr(cfg, "PATHS", p)
    def write(path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    # El arte va en subcarpetas: el sharding por hash mete las imágenes dos
    # niveles abajo y el recorrido tiene que ser recursivo.
    write(p.art_dir / "ab" / "cd" / "art.png", 5000)
    write(p.custom_art_dir / "mio.jpg", 400)
    write(p.cardbacks_dir / "back.png", 300)
    write(p.thumbs_dir / "thumb.webp", 150)
    write(p.exports_dir / "deck.pdf", 900)
    write(p.backups_dir / "mpc-forge-backup-20250101-010101-pre-migration.zip", 70)
    write(p.backups_dir / "mpc-forge-backup-20250102-010101-pre-migration.zip", 80)
    write(p.backups_dir / "mpc-forge-backup-20250103-010101.zip", 90)
    write(p.data_dir / "logs" / "mpc-forge.log.1", 60)
    # Fichero suelto en la carpeta de datos: el caso que cubre "other".
    write(p.data_dir / "tag_vocabulary.json", 25)

    # Sin teardown: `tmp_path` lo limpia pytest, y con PATHS restaurado por
    # monkeypatch no queda nada apuntando aquí.
    return p


class TestSnapshot:
    def test_measures_every_category(self, disk_content):
        sizes = {c["key"]: c["bytes"] for c in storage.compute()["categories"]}
        assert sizes["art"] == 5000
        assert sizes["custom_art"] == 400
        assert sizes["cardbacks"] == 300
        assert sizes["thumbs"] == 150
        assert sizes["exports"] == 900
        assert sizes["backups"] == 70 + 80 + 90
        assert sizes["logs"] == 60
        assert sizes["other"] == 25

    def test_total_equals_the_sum_of_its_parts(self, disk_content):
        snap = storage.compute()
        assert snap["totals"]["bytes"] == sum(c["bytes"] for c in snap["categories"])

    def test_no_category_is_left_out_of_the_breakdown(self, disk_content):
        """El desglose cubre todas las categorías declaradas, incluso vacías.

        Si una fila desapareciera, la interfaz dejaría de enseñar esa carpeta
        y su tamaño se esfumaría del total sin avisar.
        """
        keys = {c["key"] for c in storage.compute()["categories"]}
        assert keys == {c.key for c in storage.CATEGORIES}

    def test_database_counts_its_wal_sidecar(self, disk_content):
        """El WAL puede tener decenas de MB sin volcar al .sqlite3.

        Medir solo el fichero principal daría una cifra menor que la real
        justo cuando la base de datos más ha crecido.
        """
        wal = cfg.PATHS.db_path.with_name(cfg.PATHS.db_path.name + "-wal")
        wal.write_bytes(b"x" * 4096)
        try:
            db_row = next(
                c for c in storage.compute()["categories"] if c["key"] == "database"
            )
            assert db_row["bytes"] >= 4096
        finally:
            wal.unlink(missing_ok=True)

    def test_marks_what_can_be_freed(self, disk_content):
        rows = {c["key"]: c for c in storage.compute()["categories"]}
        # Recuperable: se regenera, se vuelve a descargar o ya se entregó.
        assert rows["thumbs"]["reclaimable"] and rows["thumbs"]["purge_target"] == "thumbs"
        assert rows["exports"]["reclaimable"]
        assert rows["backups"]["reclaimable"]
        assert rows["logs"]["reclaimable"]
        # Irrecuperable: no se ofrece borrarlo desde la app bajo ningún concepto.
        for key in ("database", "custom_art", "cardbacks"):
            assert not rows[key]["reclaimable"]
            assert rows[key]["purge_target"] is None

    def test_reclaimable_total_excludes_user_data(self, disk_content):
        snap = storage.compute()
        # thumbs + exports + backups + logs, nunca el arte custom ni la BD.
        assert snap["totals"]["reclaimable_bytes"] == 150 + 900 + 240 + 60

    def test_backup_estimate_covers_what_the_zip_includes(self, disk_content):
        """``create_backup()`` comprime BD + arte + custom + reversos.

        La estimación tiene que seguir a esa lista, no a un subconjunto: es lo
        que evita que el usuario lance un backup de varios GB sin saberlo.
        """
        snap = storage.compute()
        sizes = {c["key"]: c["bytes"] for c in snap["categories"]}
        assert snap["backup_estimate"]["full_bytes"] == (
            sizes["database"] + sizes["art"] + sizes["custom_art"] + sizes["cardbacks"]
        )
        assert snap["backup_estimate"]["db_only_bytes"] == sizes["database"]

    def test_counts_backups_by_origin(self, disk_content):
        backups = storage.compute()["backups"]
        assert backups["count"] == 3
        assert backups["automatic"] == 2   # las que llevan -pre-migration
        assert backups["manual"] == 1

    def test_reports_free_space_per_volume(self, disk_content):
        volumes = storage.compute()["volumes"]
        assert volumes, "sin volúmenes no se puede avisar de un disco lleno"
        for vol in volumes:
            assert vol["total_bytes"] > 0
            assert vol["free_bytes"] >= 0
            assert vol["app_bytes"] >= 0

    def test_survives_a_folder_that_is_not_there(self):
        """Una carpeta en una unidad desconectada no puede tumbar el cálculo."""
        snap = storage.compute()
        assert snap["totals"]["bytes"] >= 0


class TestNoDoubleCounting:
    def test_a_nested_folder_is_not_counted_twice(self, disk_content, monkeypatch):
        """Nada impide apuntar ``exports_dir`` dentro de ``art_dir``.

        Es una configuración legítima (ambas en el disco grande) y sin la
        exclusión explícita esos bytes aparecerían en las dos filas y otra vez
        en el total.
        """
        nested = cfg.PATHS.art_dir / "exports-anidados"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "deck.pdf").write_bytes(b"x" * 1234)
        monkeypatch.setattr(
            cfg, "PATHS", cfg.PATHS.with_overrides(exports_dir=nested)
        )
        try:
            snap = storage.compute()
            sizes = {c["key"]: c["bytes"] for c in snap["categories"]}
            assert sizes["exports"] == 1234
            # El arte sigue valiendo lo suyo: no se ha tragado la subcarpeta.
            assert sizes["art"] == 5000
            assert snap["totals"]["bytes"] == sum(c["bytes"] for c in snap["categories"])
        finally:
            (nested / "deck.pdf").unlink(missing_ok=True)
            nested.rmdir()


class TestPurge:
    def test_refuses_to_delete_what_cannot_be_recovered(self):
        for target in ("database", "custom_art", "cardbacks", "art"):
            with pytest.raises(ValueError):
                storage.purge([target])

    def test_frees_thumbnails(self, disk_content):
        result = storage.purge(["thumbs"])
        assert result["freed_bytes"] == 150
        assert not list(cfg.PATHS.thumbs_dir.rglob("*.webp"))

    def test_exports_can_keep_the_recent_ones(self, disk_content):
        """El XML recién generado se abre desde la propia interfaz.

        Borrarlo bajo los pies del usuario mientras lo está usando es
        justamente lo que evita el filtro por antigüedad.
        """
        result = storage.purge(["exports"], exports_older_than_days=30)
        assert result["freed_bytes"] == 0
        assert (cfg.PATHS.exports_dir / "deck.pdf").exists()

        result = storage.purge(["exports"], exports_older_than_days=0)
        assert result["freed_bytes"] == 900
        assert not (cfg.PATHS.exports_dir / "deck.pdf").exists()

    def test_keeps_manual_backups_when_pruning(self, disk_content):
        """Los backups manuales los creó el usuario a mano; solo él sabe cuál
        le importa. La poda es únicamente para los automáticos."""
        storage.purge(["backups"], keep_backups=1)
        remaining = sorted(p.name for p in cfg.PATHS.backups_dir.glob("*.zip"))
        assert "mpc-forge-backup-20250103-010101.zip" in remaining   # manual
        assert "mpc-forge-backup-20250102-010101-pre-migration.zip" in remaining
        assert "mpc-forge-backup-20250101-010101-pre-migration.zip" not in remaining

    def test_invalidates_the_cached_snapshot(self, disk_content):
        before = storage.compute()["totals"]["bytes"]
        storage.peek()  # no cachea: compute() es directo
        storage.purge(["thumbs"])
        assert storage.peek() is None, (
            "tras purgar, el snapshot viejo no puede seguir disponible: la "
            "interfaz enseñaría el tamaño de antes de borrar"
        )
        assert storage.compute()["totals"]["bytes"] == before - 150


class TestEndpoints:
    async def test_returns_the_breakdown(self, client):
        r = await client.get("/api/storage/")
        assert r.status_code == 200
        data = r.json()
        assert data["cached"] is False
        assert {c["key"] for c in data["categories"]} == {
            c.key for c in storage.CATEGORIES
        }
        assert "reclaimable_bytes" in data["totals"]

    async def test_second_call_is_served_from_cache(self, client):
        await client.get("/api/storage/")
        second = await client.get("/api/storage/")
        assert second.json()["cached"] is True
        # …y `refresh=true` vuelve a tocar disco.
        assert (await client.get("/api/storage/?refresh=true")).json()["cached"] is False

    async def test_purge_rejects_protected_targets(self, client):
        r = await client.post("/api/storage/purge", json={"targets": ["database"]})
        assert r.status_code == 400
        assert "database" in r.json()["detail"]

    async def test_purge_rejects_an_empty_request(self, client):
        r = await client.post("/api/storage/purge", json={"targets": []})
        assert r.status_code == 400

    async def test_purge_returns_fresh_numbers(self, client, disk_content):
        r = await client.post("/api/storage/purge", json={"targets": ["thumbs"]})
        assert r.status_code == 200
        body = r.json()
        assert body["freed_bytes"] == 150
        # El desglose viene ya recalculado en la misma respuesta para que la
        # interfaz no parpadee con cifras viejas mientras pide otro GET.
        thumbs = next(c for c in body["storage"]["categories"] if c["key"] == "thumbs")
        assert thumbs["bytes"] == 0
