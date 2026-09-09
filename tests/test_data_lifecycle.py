"""Lifecycle operations use the injected database and bounded export semantics."""

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import get_ident

import pytest
from sqlalchemy import text

from gold_monitor.config import Settings
from gold_monitor.data_lifecycle import DataLifecycleManager
from gold_monitor.models import Database


@pytest.fixture
def lifecycle(tmp_path):
    actual = tmp_path / "actual.db"
    unrelated = tmp_path / "unrelated.db"
    database = Database(f"sqlite:///{actual.as_posix()}")
    database.create_tables()
    config = Settings(
        _env_file=None,
        database_url=f"sqlite:///{unrelated.as_posix()}",
        backup_path=str(tmp_path / "backups"),
        data_retention_days=7,
    )
    manager = DataLifecycleManager(database, config=config)
    yield manager, database, actual, unrelated
    database.close()


@pytest.mark.parametrize("name", ["../outside", "..\\outside", ".", "..", "C:outside"])
async def test_backup_name_cannot_escape_backup_directory(lifecycle, name):
    manager, _, actual, _ = lifecycle
    result = await manager.backup_database(name)
    assert result["success"] is False
    assert "非法" in result["error"]
    assert not (actual.parent / "outside.db").exists()
    assert not manager._backup_path.exists()


async def test_backup_uses_injected_engine_and_copies_wal_data(lifecycle):
    manager, database, actual, unrelated = lifecycle
    with database.get_session() as session:
        session.execute(text("PRAGMA journal_mode=WAL"))
        session.commit()
    database.save_price(2000, "test")
    database.save_price(2100, "test")
    result = await manager.backup_database("snapshot")
    assert result["success"] is True
    assert result["size_bytes"] > 0
    with sqlite3.connect(result["backup_path"]) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert snapshot.execute(
            "SELECT price FROM gold_prices ORDER BY id"
        ).fetchall() == [(2000.0,), (2100.0,)]
    assert actual.exists() and not unrelated.exists()
    assert (await manager.get_stats())["database_url"] == actual.as_posix()
    listing = await manager.list_backups()
    assert [backup["name"] for backup in listing] == ["snapshot.db"]


async def test_sqlite_memory_database_can_be_backed_up(tmp_path):
    database = Database("sqlite:///:memory:")
    database.create_tables()
    database.save_price(2200)
    manager = DataLifecycleManager(database, backup_path=str(tmp_path))
    try:
        result = await manager.backup_database("memory")
        assert result["success"] is True
        with sqlite3.connect(result["backup_path"]) as snapshot:
            assert snapshot.execute("SELECT price FROM gold_prices").fetchone() == (
                2200.0,
            )
    finally:
        await database.aclose()


async def test_backup_cannot_overwrite_its_source_database(lifecycle):
    _, database, actual, _ = lifecycle
    database.save_price(2200)
    manager = DataLifecycleManager(database, backup_path=str(actual.parent))
    result = await manager.backup_database("actual")
    assert result["success"] is False
    assert "覆盖" in result["error"]
    assert database.get_latest_price().price == 2200


@pytest.mark.parametrize("format", ["json", "csv"])
async def test_exports_apply_single_date_boundaries_and_limit(lifecycle, format):
    manager, database, _, _ = lifecycle
    start = datetime(2026, 1, 1)
    for index in range(5):
        database.save_price(2000 + index, timestamp=start + timedelta(days=index))
    export = manager.export_json if format == "json" else manager.export_csv

    def prices(content):
        if format == "json":
            return [record["price"] for record in json.loads(content)["records"]]
        return [float(row["price"]) for row in csv.DictReader(io.StringIO(content))]

    assert prices(await export(start=start + timedelta(days=2), limit=2)) == [
        2002,
        2003,
    ]
    assert prices(await export(end=start + timedelta(days=1), limit=1)) == [2000]
    assert prices(
        await export(start=start, end=start + timedelta(days=4), limit=2)
    ) == [2000, 2001]
    # Preserve the existing newest-first export when no date filters are present.
    assert prices(await export(limit=2)) == [2004, 2003]
    with pytest.raises(ValueError):
        await export(start=start + timedelta(days=2), end=start)
    with pytest.raises(ValueError):
        await export(limit=0)


async def test_exports_normalize_timezone_and_bound_query_in_database(
    lifecycle, monkeypatch
):
    manager, database, _, _ = lifecycle
    database.save_price(2000, timestamp=datetime(2026, 1, 1))
    calls = []
    original = database.get_prices_in_range

    def record_query(start, end, limit):
        calls.append((start, end, limit, get_ident()))
        return original(start, end, limit)

    monkeypatch.setattr(database, "get_prices_in_range", record_query)
    bound = datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    data = json.loads(await manager.export_json(start=bound, limit=1))
    assert data["records"][0]["timestamp"] == "2026-01-01T00:00:00Z"
    assert calls[0][:3] == (datetime(2026, 1, 1), datetime.max, 1)
    assert calls[0][3] != get_ident()


async def test_cleanup_uses_instance_retention_configuration(lifecycle):
    manager, database, _, unrelated = lifecycle
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    database.save_price(2000, timestamp=now - timedelta(days=8))
    database.save_price(2100, timestamp=now - timedelta(days=6))
    result = await manager.cleanup()
    assert result["deleted_count"] == 1 and result["archived_count"] == 0
    assert database.get_latest_price().price == 2100
    assert not unrelated.exists()


async def test_restore_requires_closed_database_and_uses_engine_target(lifecycle):
    manager, database, actual, unrelated = lifecycle
    database.save_price(2000)
    backup = await manager.backup_database("restore")
    database.save_price(2100)
    online = await manager.restore_backup(backup["backup_path"])
    assert online["success"] is False
    assert "db.aclose()" in online["error"]
    assert database.get_price_count() == 2
    await database.aclose()
    assert database.closed
    restored = await manager.restore_backup(backup["backup_path"])
    assert restored["success"] is True
    assert not unrelated.exists()
    with sqlite3.connect(actual) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gold_prices").fetchone() == (1,)
    with sqlite3.connect(f"{actual}.bak") as previous:
        assert previous.execute("SELECT COUNT(*) FROM gold_prices").fetchone() == (2,)


async def test_restore_rejects_non_sqlite_file_without_changing_target(lifecycle):
    manager, database, actual, _ = lifecycle
    database.save_price(2000)
    invalid = actual.parent / "invalid.db"
    invalid.write_text("not a sqlite database", encoding="utf-8")
    await database.aclose()
    result = await manager.restore_backup(str(invalid))
    assert result["success"] is False
    assert "SQLite" in result["error"]
    with sqlite3.connect(actual) as connection:
        assert connection.execute("SELECT price FROM gold_prices").fetchone() == (
            2000.0,
        )
