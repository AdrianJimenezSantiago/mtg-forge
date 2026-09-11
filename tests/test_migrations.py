"""Tests del motor de migraciones.

El objetivo de estos tests es blindar la propiedad más importante del sistema:
**una actualización de la app nunca borra el trabajo del usuario**. El sistema
anterior hacía ``drop_all`` cuando cambiaba ``SCHEMA_VERSION``; estos tests
fallarían inmediatamente si alguien reintrodujera ese comportamiento.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mpc_forge import migrations


async def _fresh_engine(tmp_path: Path, name: str = "t.sqlite3"):
    """Un engine async sobre una BD SQLite temporal y aislada."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    return engine


class TestLadderIntegrity:
    """Invariantes estructurales del ladder. Fallan al escribir la migración,
    no en producción seis meses después."""

    def test_versions_are_strictly_increasing(self):
        versions = [m.version for m in migrations.MIGRATIONS]
        assert versions == sorted(versions), "El ladder debe estar ordenado"
        assert len(versions) == len(set(versions)), "Hay versiones duplicadas"

    def test_versions_start_above_baseline(self):
        for m in migrations.MIGRATIONS:
            assert m.version > migrations.BASELINE_VERSION, (
                f"La migración {m.version} pisa el baseline. Las migraciones "
                f"nuevas deben empezar en {migrations.BASELINE_VERSION + 1}."
            )

    def test_no_gaps_in_ladder(self):
        versions = [m.version for m in migrations.MIGRATIONS]
        expected = list(range(
            migrations.BASELINE_VERSION + 1,
            migrations.BASELINE_VERSION + 1 + len(versions),
        ))
        assert versions == expected, "El ladder tiene huecos"

    def test_every_migration_has_a_description(self):
        for m in migrations.MIGRATIONS:
            assert m.description.strip(), f"La migración {m.version} no se describe"

    def test_no_destructive_statements(self):
        """Ninguna migración puede contener DROP TABLE ni DELETE sin WHERE.

        Este es el guardián: si alguien vuelve a meter un borrado masivo, el
        test lo caza antes del release.
        """
        for m in migrations.MIGRATIONS:
            for stmt in m.statements:
                normalized = " ".join(stmt.lower().split())
                assert "drop table" not in normalized, (
                    f"Migración {m.version}: DROP TABLE está prohibido"
                )
                assert "drop column" not in normalized, (
                    f"Migración {m.version}: DROP COLUMN está prohibido"
                )
                if "delete from" in normalized:
                    assert "where" in normalized, (
                        f"Migración {m.version}: DELETE sin WHERE está prohibido"
                    )


class TestFreshDatabase:
    async def test_fresh_db_is_stamped_at_latest(self, tmp_path):
        engine = await _fresh_engine(tmp_path)
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT)"
            ))
            # Sin fila schema_version → read_version devuelve None → BD nueva.
            report = await migrations.run(conn)
        assert report["fresh"] is True
        assert report["to_version"] == migrations.LATEST_VERSION
        await engine.dispose()

    async def test_fresh_db_does_not_create_a_backup(self, tmp_path):
        """Una instalación nueva no debe generar un zip de backup vacío."""
        calls = []
        engine = await _fresh_engine(tmp_path)
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT)"
            ))
            await migrations.run(conn, on_backup=lambda: calls.append(1) or "x.zip")
        assert calls == [], "No se debe hacer backup de una BD recién creada"
        await engine.dispose()


class TestLegacyAdoption:
    """El caso crítico: una BD del sistema antiguo debe conservar sus datos."""

    async def test_legacy_db_keeps_its_decks(self, tmp_path):
        db = tmp_path / "legacy.sqlite3"
        # Simulamos una BD escrita por una versión vieja de la app: esquema
        # mínimo, versión 3, y datos reales del usuario dentro.
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '3');
            CREATE TABLE decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(256),
                format VARCHAR(32) DEFAULT 'commander',
                imported_at DATETIME,
                updated_at DATETIME
            );
            INSERT INTO decks (name) VALUES ('Atraxa Superfriends');
            INSERT INTO decks (name) VALUES ('Krenko Goblins');
        """)
        conn.commit()
        conn.close()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            report = await migrations.run(c)
        await engine.dispose()

        # Los mazos siguen ahí. Esto es lo que el sistema antiguo destruía.
        conn = sqlite3.connect(db)
        names = [r[0] for r in conn.execute("SELECT name FROM decks ORDER BY id")]
        version = conn.execute(
            "SELECT value FROM kv_store WHERE key='schema_version'"
        ).fetchone()[0]
        conn.close()

        assert names == ["Atraxa Superfriends", "Krenko Goblins"]
        assert int(version) == migrations.LATEST_VERSION
        assert "adopt-legacy" in report["applied"]

    async def test_legacy_adoption_triggers_a_backup(self, tmp_path):
        db = tmp_path / "legacy2.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '2');
        """)
        conn.commit()
        conn.close()

        made = []
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            await migrations.run(c, on_backup=lambda: (made.append(1), "b.zip")[1])
        await engine.dispose()
        assert made, "Una migración sobre una BD con datos debe hacer backup"


class TestIncrementalUpgrade:
    async def test_upgrade_from_baseline_applies_all_pending(self, tmp_path):
        db = tmp_path / "v8.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES
                ('schema_version', '{migrations.BASELINE_VERSION}');
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name VARCHAR(256));
            INSERT INTO decks (name) VALUES ('Mi mazo');
            CREATE TABLE local_arts (
                id INTEGER PRIMARY KEY, sha256 VARCHAR(64), relative_path TEXT
            );
        """)
        conn.commit()
        conn.close()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            report = await migrations.run(c)
        await engine.dispose()

        assert report["to_version"] == migrations.LATEST_VERSION
        conn = sqlite3.connect(db)
        # La migración 9 crea deck_snapshots y añade local_arts.thumb_path.
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(local_arts)")}
        assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 1
        conn.close()

        assert "deck_snapshots" in tables
        assert "art_themes" in tables
        assert "bulk_sync_state" in tables
        assert "thumb_path" in cols

    async def test_running_twice_is_idempotent(self, tmp_path):
        db = tmp_path / "twice.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES
                ('schema_version', '{migrations.BASELINE_VERSION}');
            CREATE TABLE local_arts (id INTEGER PRIMARY KEY, sha256 VARCHAR(64));
        """)
        conn.commit()
        conn.close()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            await migrations.run(c)
        async with engine.begin() as c:
            second = await migrations.run(c)
        await engine.dispose()
        assert second["applied"] == [], "La segunda pasada no debe aplicar nada"

    async def test_newer_db_than_app_is_left_alone(self, tmp_path):
        """Si el usuario abre una BD nueva con una app vieja, no la tocamos."""
        db = tmp_path / "future.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES
                ('schema_version', '{migrations.LATEST_VERSION + 5}');
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name VARCHAR(256));
            INSERT INTO decks (name) VALUES ('Del futuro');
        """)
        conn.commit()
        conn.close()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            report = await migrations.run(c)
        await engine.dispose()

        assert report["applied"] == []
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 1
        conn.close()


class TestFailureIsolation:
    async def test_failed_migration_rolls_back_and_keeps_version(
        self, tmp_path, monkeypatch
    ):
        """Una migración rota no debe dejar la BD a medias ni impedir arrancar."""
        db = tmp_path / "broken.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES
                ('schema_version', '{migrations.BASELINE_VERSION}');
        """)
        conn.commit()
        conn.close()

        bad = migrations.Migration(
            version=migrations.BASELINE_VERSION + 1,
            description="migración deliberadamente rota",
            statements=[
                "CREATE TABLE ok_before (id INTEGER PRIMARY KEY)",
                "ESTO NO ES SQL VÁLIDO",
            ],
        )
        monkeypatch.setattr(migrations, "MIGRATIONS", [bad])

        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as c:
            report = await migrations.run(c)
        await engine.dispose()

        assert report.get("failed_at") == bad.version
        conn = sqlite3.connect(db)
        version = int(conn.execute(
            "SELECT value FROM kv_store WHERE key='schema_version'"
        ).fetchone()[0])
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()

        assert version == migrations.BASELINE_VERSION, (
            "La versión no debe avanzar si la migración falló"
        )
        assert "ok_before" not in tables, (
            "El savepoint debe deshacer las sentencias que sí funcionaron"
        )

    def test_backup_failure_does_not_block_startup(self, caplog):
        def explode():
            raise OSError("disco lleno")
        assert migrations._safe_backup(explode) is None


class TestNoDropAllRemains:
    def test_db_module_has_no_drop_all(self):
        """Guardián contra la regresión más cara del proyecto."""
        source = (Path(__file__).parent.parent / "mpc_forge" / "db.py").read_text(
            encoding="utf-8"
        )
        assert "drop_all" not in source, (
            "db.py ha vuelto a contener drop_all. Ese código borraba los mazos "
            "del usuario en cada cambio de esquema."
        )

    def test_migrations_module_has_no_drop_all(self):
        source = (
            Path(__file__).parent.parent / "mpc_forge" / "migrations.py"
        ).read_text(encoding="utf-8")
        # El módulo menciona drop_all en la documentación explicando por qué
        # no lo usa; lo que no puede haber es una llamada real.
        assert "run_sync(Base.metadata.drop_all" not in source
        assert ".drop_all(" not in source
