"""Data retention, bounded exports and database-aware backup operations."""

import asyncio
from contextlib import closing
import csv
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Optional, TypedDict

from .config import Settings, settings
from .models import Database, GoldPrice
from .time_utils import iso_utc, storage_time, utcnow

logger = logging.getLogger(__name__)
_SAFE_BACKUP_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class BackupResult(TypedDict, total=False):
    success: bool
    backup_path: str | None
    size_bytes: int
    backup_time: str
    error: str
    backup_scope: str
    record_limit: int


class BackupInfo(TypedDict):
    name: str
    path: str
    size_bytes: int
    created_at: str


def _atomic_file(destination: Path, write: Callable[[Path], None]) -> None:
    """Publish a complete file only after its writer succeeds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=destination.parent, suffix=".tmp", delete=False
    ) as file:
        temporary = Path(file.name)
    try:
        write(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class DataLifecycleManager:
    def __init__(
        self,
        database: Database,
        retention_days: int | None = None,
        hourly_aggregation_days: int | None = None,
        daily_aggregation_days: int | None = None,
        backup_path: str | None = None,
        config: Settings | None = None,
    ):
        config = config if config is not None else settings
        self._db = database
        self._retention_days = (
            config.data_retention_days if retention_days is None else retention_days
        )
        self._hourly_days = (
            config.hourly_aggregation_days
            if hourly_aggregation_days is None
            else hourly_aggregation_days
        )
        self._daily_days = (
            config.daily_aggregation_days
            if daily_aggregation_days is None
            else daily_aggregation_days
        )
        if self._retention_days < 1:
            raise ValueError("数据保留天数不能小于 1")
        self._backup_path = Path(
            config.backup_path if backup_path is None else backup_path
        )

    async def cleanup(self) -> dict:
        """Delete expired raw prices. Aggregation remains unsupported."""
        result = {
            "deleted_count": 0,
            "archived_count": 0,
            "cleanup_time": iso_utc(utcnow()),
        }
        try:
            cutoff = utcnow() - timedelta(days=self._retention_days)
            result["deleted_count"] = await self._db.run(
                self._db.delete_old_prices, cutoff
            )
        except Exception as error:
            logger.exception("数据清理失败")
            result["error"] = str(error)
        return result

    async def get_stats(self) -> dict:
        try:
            total_count = await self._db.run(self._db.get_price_count)
            oldest = await self._db.run(self._db.get_oldest_price)
            latest = await self._db.run(self._db.get_latest_price)
            return {
                "total_records": total_count,
                "oldest_record": iso_utc(oldest.timestamp) if oldest else None,
                "latest_record": iso_utc(latest.timestamp) if latest else None,
                "retention_days": self._retention_days,
                "database_url": self._db.engine.url.database
                if self._is_sqlite
                else "remote",
            }
        except Exception as error:
            logger.exception("获取数据统计失败")
            return {"error": str(error)}

    async def _export_records(
        self,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[GoldPrice]:
        if start is None and end is None:
            return await self._db.run(self._db.get_recent_prices, limit)
        lower = storage_time(start) if start is not None else datetime.min
        upper = storage_time(end) if end is not None else datetime.max
        if lower > upper:
            raise ValueError("start 不能晚于 end")
        return await self._db.run(self._db.get_prices_in_range, lower, upper, limit)

    @staticmethod
    def _csv(records: list[GoldPrice]) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "price", "currency", "source", "timestamp"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    record.price,
                    record.currency,
                    record.source,
                    iso_utc(record.timestamp),
                ]
            )
        return output.getvalue()

    @staticmethod
    def _json(records: list[GoldPrice]) -> str:
        return json.dumps(
            {
                "export_time": iso_utc(utcnow()),
                "record_count": len(records),
                "records": [
                    {
                        "id": record.id,
                        "price": record.price,
                        "currency": record.currency,
                        "source": record.source,
                        "timestamp": iso_utc(record.timestamp),
                    }
                    for record in records
                ],
            },
            indent=2,
            ensure_ascii=False,
        )

    async def export_csv(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10000,
    ) -> str:
        records = await self._export_records(start, end, limit)
        return await asyncio.to_thread(self._csv, records)

    async def export_json(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10000,
    ) -> str:
        records = await self._export_records(start, end, limit)
        return await asyncio.to_thread(self._json, records)

    @property
    def _is_sqlite(self) -> bool:
        return self._db.engine.url.get_backend_name() == "sqlite"

    def _sqlite_path(self) -> Path | None:
        name = self._db.engine.url.database
        if not self._is_sqlite or name in (None, "", ":memory:"):
            return None
        assert name is not None
        return Path(name).resolve()

    def _backup_destination(
        self, name: str, extension: str, automatic: bool = False
    ) -> Path:
        if (
            name in {".", ".."}
            or Path(name).name != name
            or not _SAFE_BACKUP_NAME.fullmatch(name)
        ):
            raise ValueError("非法备份名称：只能使用字母、数字、点、下划线和短横线")
        root = (
            self._backup_path / "automatic" if automatic else self._backup_path
        ).resolve()
        destination = (root / f"{name}{extension}").resolve()
        if destination.parent != root or destination == self._sqlite_path():
            raise ValueError("非法备份名称：备份不能覆盖当前数据库或写出备份目录")
        return destination

    def _backup_sqlite(self, destination: Path) -> int:
        """Runs inside db.run; use the injected connection, including memory/WAL DBs."""

        def write(temporary: Path) -> None:
            with self._db.get_session() as session:
                source = session.connection().connection.driver_connection
                if not isinstance(source, sqlite3.Connection):
                    raise TypeError("SQLite 驱动不支持 backup API")
                with closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)

        _atomic_file(destination, write)
        return destination.stat().st_size

    async def backup_database(
        self, backup_name: str | None = None, *, automatic: bool = False
    ) -> BackupResult:
        result: BackupResult = {
            "success": False,
            "backup_path": None,
            "size_bytes": 0,
            "backup_time": iso_utc(utcnow()),
        }
        try:
            if automatic and backup_name is not None:
                raise ValueError("自动备份名称由应用生成")
            name = backup_name or f"gold_prices_{utcnow().strftime('%Y%m%d_%H%M%S_%f')}"
            extension = ".db" if self._is_sqlite else ".json"
            destination = await asyncio.to_thread(
                self._backup_destination, name, extension, automatic
            )
            if self._is_sqlite:
                result["size_bytes"] = await self._db.run(
                    self._backup_sqlite, destination
                )
            else:
                # Retain the historical non-SQLite price export, and identify its scope.
                data = await self.export_json(limit=100000)

                def write_export() -> int:
                    def write(temporary: Path) -> None:
                        temporary.write_text(data, encoding="utf-8")

                    _atomic_file(destination, write)
                    return destination.stat().st_size

                result["size_bytes"] = await asyncio.to_thread(write_export)
                result["backup_scope"] = "prices_only"
                result["record_limit"] = 100000
            result["success"] = True
            result["backup_path"] = str(destination)
        except Exception as error:
            logger.exception("数据库备份失败")
            result["error"] = str(error)
        return result

    def _prune_automatic_backups(self, keep_count: int, newest: Path) -> None:
        root = (self._backup_path / "automatic").resolve()
        if not root.is_dir():
            return
        generated = re.compile(r"^gold_prices_\d{8}_\d{6}_\d{6}\.(db|json)$")
        files = sorted(
            (
                file
                for file in root.iterdir()
                if file.is_file()
                and not file.is_symlink()
                and file.resolve().parent == root
                and file.resolve() != self._sqlite_path()
                and generated.fullmatch(file.name)
            ),
            # A clock adjustment must never delete the just-created snapshot.
            key=lambda file: (file.resolve() == newest.resolve(), file.name),
            reverse=True,
        )
        for file in files[keep_count:]:
            file.unlink()

    async def automatic_backup(self, keep_count: int = 7) -> BackupResult:
        """Rotate only app-generated snapshots, and only after a successful backup."""
        if keep_count < 1:
            raise ValueError("自动备份至少保留一份")
        result = await self.backup_database(automatic=True)
        if result["success"]:
            assert result["backup_path"] is not None
            await asyncio.to_thread(
                self._prune_automatic_backups, keep_count, Path(result["backup_path"])
            )
        return result

    def _list_backups(self) -> list[BackupInfo]:
        backups: list[BackupInfo] = []
        if not self._backup_path.exists():
            return backups
        files = list(self._backup_path.iterdir())
        automatic = self._backup_path / "automatic"
        if automatic.is_dir():
            files.extend(automatic.iterdir())
        for file in files:
            if file.is_file() and file.suffix in (".db", ".json"):
                stat = file.stat()
                backups.append(
                    {
                        "name": file.relative_to(self._backup_path).as_posix(),
                        "path": str(file),
                        "size_bytes": stat.st_size,
                        "created_at": iso_utc(
                            datetime.fromtimestamp(stat.st_ctime, timezone.utc)
                        ),
                    }
                )
        return sorted(backups, key=lambda backup: backup["created_at"], reverse=True)

    async def list_backups(self) -> list[BackupInfo]:
        try:
            return await asyncio.to_thread(self._list_backups)
        except Exception:
            logger.exception("列出备份失败")
            return []

    @staticmethod
    def _validate_backup(source: Path) -> None:
        if not source.is_file() or source.suffix.lower() != ".db":
            raise ValueError("备份文件必须是存在的 SQLite .db 文件")
        with source.open("rb") as file:
            if file.read(16) != b"SQLite format 3\x00":
                raise ValueError("备份不是有效的 SQLite 数据库")
        with closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite 备份完整性检查失败")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "gold_prices" not in tables:
                raise ValueError("备份缺少 gold_prices 表")

    def _restore_offline(self, source: Path) -> None:
        destination = self._sqlite_path()
        if destination is None:
            raise ValueError("只能离线恢复到文件型 SQLite 数据库")
        source = source.resolve()
        if source == destination:
            raise ValueError("备份文件不能是当前数据库")
        self._validate_backup(source)
        # No app workers/connections may be active. Keep a recoverable pre-restore copy.
        if destination.exists():

            def write_current(temporary: Path) -> None:
                with closing(sqlite3.connect(destination)) as current:
                    with closing(sqlite3.connect(temporary)) as previous:
                        current.backup(previous)

            _atomic_file(Path(f"{destination}.bak"), write_current)
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as backup:
            with closing(sqlite3.connect(destination)) as target:
                backup.backup(target)

    async def restore_backup(self, backup_path: str) -> dict:
        """Offline only: stop all writers/processes and await db.aclose() first.

        This instance must remain closed. Create a new Database after restoration.
        No HTTP restore route is registered.
        """
        result = {
            "success": False,
            "restored_from": backup_path,
            "restore_time": iso_utc(utcnow()),
        }
        try:
            if not self._db.closed:
                raise ValueError("恢复前必须停止所有数据库使用者并调用 db.aclose()")
            await asyncio.to_thread(self._restore_offline, Path(backup_path))
            result["success"] = True
        except Exception as error:
            logger.exception("数据库恢复失败")
            result["error"] = str(error)
        return result


# ============ 定时清理任务 ============

_cleanup_task: Optional[asyncio.Task] = None


async def _cleanup_scheduler(manager: DataLifecycleManager, interval_hours: int = 24):
    """定时清理调度器"""
    while True:
        try:
            # 等待到下一个清理时间
            await asyncio.sleep(interval_hours * 3600)

            # 执行清理
            logger.info("开始执行定时数据清理...")
            result = await manager.cleanup()
            logger.info("定时清理完成: %s", result)

        except asyncio.CancelledError:
            logger.info("定时清理任务已取消")
            break
        except Exception as e:
            logger.error("定时清理出错: %s", e)
            await asyncio.sleep(3600)  # 出错后等待1小时


def start_cleanup_scheduler(manager: DataLifecycleManager, interval_hours: int = 24):
    """旧调用兼容；应用 runtime 自己拥有清理任务，不调用此全局入口。"""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_scheduler(manager, interval_hours))
        logger.info("定时清理任务已启动（间隔: %d 小时）", interval_hours)


def stop_cleanup_scheduler():
    """停止定时清理任务"""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()


# ============ 全局实例 ============

_lifecycle_manager: Optional[DataLifecycleManager] = None


def get_lifecycle_manager(
    database: Database | None = None,
) -> Optional[DataLifecycleManager]:
    """旧调用兼容；新应用从 runtime 获取自己的管理器。"""
    global _lifecycle_manager
    if _lifecycle_manager is None and database:
        _lifecycle_manager = DataLifecycleManager(database)
    return _lifecycle_manager


def set_lifecycle_manager(manager: DataLifecycleManager):
    """设置全局生命周期管理器"""
    global _lifecycle_manager
    _lifecycle_manager = manager
