from __future__ import annotations

import asyncio
import logging

import pytest

from mpc_forge.services.system import logging_setup
from mpc_forge.utils.background import BackgroundTasks


class TestBackgroundTasks:
    async def test_failures_are_logged_and_tasks_forgotten(self, caplog):
        tasks = BackgroundTasks()

        async def boom():
            raise RuntimeError("bad")

        async def ok():
            return 1

        with caplog.at_level(logging.ERROR):
            t1 = tasks.spawn(boom(), name="boom")
            t2 = tasks.spawn(ok(), name="ok")
            await asyncio.gather(t1, t2, return_exceptions=True)
            await asyncio.sleep(0)
        assert "boom" in caplog.text and not tasks._tasks
        await tasks.shutdown()

    async def test_shutdown_cancels_pending_work(self):
        tasks = BackgroundTasks()
        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(3600)

        task = tasks.spawn(forever(), name="forever")
        await started.wait()
        await tasks.shutdown(timeout=1)
        assert task.cancelled()

    async def test_shutdown_gives_up_on_stubborn_tasks(self, caplog):
        tasks = BackgroundTasks()
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue

        task = tasks.spawn(stubborn(), name="stubborn")
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING):
            await tasks.shutdown(timeout=0.05)
        assert "no respondieron" in caplog.text
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestFileLogging:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        logging_setup.teardown_file_logging(delete=True)

    def test_file_log_is_redacted_tailed_and_removed(self, tmp_path):
        assert logging_setup.read_tail() == ""
        path = logging_setup.setup_file_logging(tmp_path / "logs")
        assert logging_setup.current_log_path() == path
        logger = logging.getLogger("mpc_forge.test")
        logger.info("calling https://x.test/?key=AIzaSyD-SecretKeyForTests-0123456789 ok")
        try:
            raise ValueError("token=abc123")
        except ValueError:
            logger.exception("Authorization: Bearer topsecret")
        for i in range(5):
            logger.info("line %d", i)
        for handler in logging.getLogger().handlers:
            handler.flush()

        text = path.read_text(encoding="utf-8")
        assert "AIza" not in text and "topsecret" not in text and "abc123" not in text
        assert "[REDACTED]" in text
        assert logging_setup.read_tail(2).splitlines()[-1].endswith("line 4")

        again = logging_setup.setup_file_logging(tmp_path / "logs")
        assert again == path and "line 4" not in path.read_text(encoding="utf-8")
        logging_setup.teardown_file_logging(delete=True)
        assert not path.exists() and logging_setup.current_log_path() is None

    def test_unformattable_records_pass_through(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("not a number",), None)
        assert logging_setup.RedactSecretsFilter().filter(record) is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("password=hunter2&x=1", "password=[REDACTED]&x=1"),
            ("Authorization: Basic Zm9v", "Authorization: Basic [REDACTED]"),
            ("nothing here", "nothing here"),
        ],
    )
    def test_redact(self, raw, expected):
        assert logging_setup.redact(raw) == expected

    async def test_debug_endpoints(self, client, tmp_path):
        assert (await client.get("/api/debug/log/info")).json()["exists"] is False
        assert (await client.get("/api/debug/log/download")).status_code == 404
        logging_setup.setup_file_logging(tmp_path / "logs")
        logging.getLogger("mpc_forge.test").info("hello from the test")
        for handler in logging.getLogger().handlers:
            handler.flush()
        info = (await client.get("/api/debug/log/info")).json()
        assert info["exists"] is True and info["size_bytes"] > 0
        assert "hello from the test" in (await client.get("/api/debug/log/tail?n=5")).text
        r = await client.get("/api/debug/log/download")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
