"""Automatic snapshots are bounded and kept separate from manual backups."""

import asyncio
from pathlib import Path
import sqlite3
from datetime import datetime

import pytest

from gold_monitor.data_lifecycle import DataLifecycleManager
from gold_monitor.models import Database
from gold_monitor.runtime import ApplicationRuntime


async def test_rotation_keeps_current_snapshot_when_clock_moves_back(
    tmp_path, app_settings, monkeypatch
):
    db = Database("sqlite:///:memory:")
    await db.run(db.create_tables)
    manager = DataLifecycleManager(db, backup_path=str(tmp_path), config=app_settings)
    try:
        monkeypatch.setattr(
            "gold_monitor.data_lifecycle.utcnow", lambda: datetime(2026, 9, 10, 8)
        )
        older = await manager.automatic_backup(keep_count=1)
        monkeypatch.setattr(
            "gold_monitor.data_lifecycle.utcnow", lambda: datetime(2026, 9, 10, 7)
        )
        current = await manager.automatic_backup(keep_count=1)
        assert current["success"] and Path(current["backup_path"]).exists()
        assert not Path(older["backup_path"]).exists()
    finally:
        await db.aclose()


async def test_rotation_never_removes_the_active_database(tmp_path, app_settings):
    directory = tmp_path / "automatic"
    directory.mkdir()
    active = directory / "gold_prices_19990101_000000_000000.db"
    db = Database(f"sqlite:///{active.as_posix()}")
    await db.run(db.create_tables)
    await db.run(db.save_price, 2300)
    manager = DataLifecycleManager(db, backup_path=str(tmp_path), config=app_settings)
    try:
        result = await manager.automatic_backup(keep_count=1)
        assert result["success"] and Path(result["backup_path"]).exists()
        assert active.exists()
        assert await db.run(db.get_price_count) == 1
    finally:
        await db.aclose()


async def test_automatic_backups_rotate_only_after_success(
    tmp_path, app_settings, monkeypatch
):
    db = Database("sqlite:///:memory:")
    await db.run(db.create_tables)
    manager = DataLifecycleManager(db, backup_path=str(tmp_path), config=app_settings)
    try:
        manual = await manager.backup_database("manual")
        generated = []
        for price in (2300, 2301, 2302):
            await db.run(db.save_price, price)
            result = await manager.automatic_backup(keep_count=2)
            assert result["success"]
            generated.append(Path(result["backup_path"]))
        assert Path(manual["backup_path"]).exists()
        assert not generated[0].exists()
        assert all(file.exists() for file in generated[1:])
        with sqlite3.connect(generated[-1]) as snapshot:
            assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert snapshot.execute("SELECT count(*) FROM gold_prices").fetchone() == (
                3,
            )
        names = {item["name"] for item in await manager.list_backups()}
        assert "manual.db" in names
        assert sum(name.startswith("automatic/") for name in names) == 2

        def fail_backup(destination):
            raise OSError("injected disk failure")

        monkeypatch.setattr(manager, "_backup_sqlite", fail_backup)
        assert not (await manager.automatic_backup(keep_count=1))["success"]
        assert all(file.exists() for file in generated[1:])
        assert Path(manual["backup_path"]).exists()
    finally:
        await db.aclose()


@pytest.mark.parametrize("enabled", [False, True])
async def test_runtime_owns_automatic_backup_task(app_settings, monkeypatch, enabled):
    completed = asyncio.Event()
    results = []
    original = DataLifecycleManager.automatic_backup

    async def backup(manager, keep_count):
        result = await original(manager, keep_count)
        results.append(result)
        completed.set()
        return result

    monkeypatch.setattr(DataLifecycleManager, "automatic_backup", backup)
    runtime = ApplicationRuntime(
        app_settings.model_copy(update={"backup_enabled": enabled})
    )
    try:
        await runtime.start()
        if enabled:
            await asyncio.wait_for(completed.wait(), timeout=5)
            assert results[0]["success"]
            assert Path(results[0]["backup_path"]).parent.name == "automatic"
        else:
            # Only cleanup and daily/startup analysis were scheduled.
            assert not any(
                task.get_coro().__name__ == "_backup_loop" for task in runtime.tasks
            )
    finally:
        await runtime.close()
    assert not runtime.tasks
