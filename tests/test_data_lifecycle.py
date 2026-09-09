"""数据生命周期安全测试"""

import shutil
import uuid
from pathlib import Path

import pytest

from gold_monitor.config import settings
from gold_monitor.data_lifecycle import DataLifecycleManager
from gold_monitor.models import Database


@pytest.mark.asyncio
async def test_backup_name_cannot_escape_backup_directory(monkeypatch):
    """备份名不能通过 ../ 写到备份目录之外。"""
    tmp_root = Path(".tmp_pytest") / f"backup-{uuid.uuid4().hex}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        db_file = tmp_root / "gold.db"
        database_url = f"sqlite:///{db_file.resolve().as_posix()}"
        database = Database(database_url)
        database.create_tables()
        database.save_price(2000.0, "test")
        monkeypatch.setattr(settings, "database_url", database_url)

        manager = DataLifecycleManager(database, backup_path=str(tmp_root / "backups"))

        result = await manager.backup_database("../outside")

        assert result["success"] is False
        assert "非法" in result["error"]
        assert not (tmp_root / "outside.db").exists()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
