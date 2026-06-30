"""数据生命周期管理模块 - 归档、清理、导出、备份"""

import asyncio
import csv
import io
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

from .config import settings
from .models import Database

logger = logging.getLogger(__name__)


class BackupResult(TypedDict, total=False):
    success: bool
    backup_path: str | None
    size_bytes: int
    backup_time: str
    error: str


class BackupInfo(TypedDict):
    name: str
    path: str
    size_bytes: int
    created_at: str


class DataLifecycleManager:
    """数据生命周期管理器"""

    def __init__(
        self,
        database: Database,
        retention_days: int | None = None,
        hourly_aggregation_days: int | None = None,
        daily_aggregation_days: int | None = None,
        backup_path: str | None = None,
    ):
        self._db = database
        self._retention_days = retention_days or settings.data_retention_days
        self._hourly_days = hourly_aggregation_days or settings.hourly_aggregation_days
        self._daily_days = daily_aggregation_days or settings.daily_aggregation_days
        self._backup_path = Path(backup_path or settings.backup_path)

    # ============ 数据清理 ============

    async def cleanup(self) -> dict:
        """执行数据清理任务

        Returns:
            {"deleted_count": int, "archived_count": int}
        """
        result = {
            "deleted_count": 0,
            "archived_count": 0,
            "cleanup_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }

        try:
            # 1. 删除超过保留期的分钟级数据
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                days=self._retention_days
            )
            deleted = self._db.delete_old_prices(cutoff)
            result["deleted_count"] = deleted
            logger.info(
                "清理完成: 删除 %d 条旧数据（%d 天前）", deleted, self._retention_days
            )

            # TODO: 实现数据聚合（分钟级 -> 小时级 -> 日级）
            # 这需要创建额外的聚合表

        except Exception as e:
            logger.error("数据清理失败: %s", e)
            result["error"] = str(e)

        return result

    async def get_stats(self) -> dict:
        """获取数据统计信息"""
        try:
            total_count = self._db.get_price_count()
            oldest = self._db.get_oldest_price()
            latest = self._db.get_latest_price()

            return {
                "total_records": total_count,
                "oldest_record": oldest.timestamp.isoformat() if oldest else None,
                "latest_record": latest.timestamp.isoformat() if latest else None,
                "retention_days": self._retention_days,
                "database_url": settings.database_url.split("///")[-1]
                if "sqlite" in settings.database_url
                else "remote",
            }
        except Exception as e:
            logger.error("获取数据统计失败: %s", e)
            return {"error": str(e)}

    # ============ 数据导出 ============

    async def export_csv(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10000,
    ) -> str:
        """导出数据为 CSV 格式

        Args:
            start: 起始时间
            end: 结束时间
            limit: 最大记录数

        Returns:
            CSV 字符串
        """
        try:
            if start and end:
                records = self._db.get_prices_in_range(start, end)
            else:
                records = self._db.get_recent_prices(limit)

            output = io.StringIO()
            writer = csv.writer(output)

            # 写入表头
            writer.writerow(["id", "price", "currency", "source", "timestamp"])

            # 写入数据
            for record in records:
                writer.writerow(
                    [
                        record.id,
                        record.price,
                        record.currency,
                        record.source,
                        record.timestamp.isoformat(),
                    ]
                )

            logger.info("导出 CSV: %d 条记录", len(records))
            return output.getvalue()

        except Exception as e:
            logger.error("CSV 导出失败: %s", e)
            raise

    async def export_json(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10000,
    ) -> str:
        """导出数据为 JSON 格式

        Args:
            start: 起始时间
            end: 结束时间
            limit: 最大记录数

        Returns:
            JSON 字符串
        """
        try:
            if start and end:
                records = self._db.get_prices_in_range(start, end)
            else:
                records = self._db.get_recent_prices(limit)

            data = {
                "export_time": datetime.now(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(),
                "record_count": len(records),
                "records": [
                    {
                        "id": r.id,
                        "price": r.price,
                        "currency": r.currency,
                        "source": r.source,
                        "timestamp": r.timestamp.isoformat(),
                    }
                    for r in records
                ],
            }

            logger.info("导出 JSON: %d 条记录", len(records))
            return json.dumps(data, indent=2, ensure_ascii=False)

        except Exception as e:
            logger.error("JSON 导出失败: %s", e)
            raise

    # ============ 数据备份 ============

    async def backup_database(self, backup_name: str | None = None) -> BackupResult:
        """备份数据库

        Args:
            backup_name: 备份文件名（不含扩展名）

        Returns:
            {"success": bool, "backup_path": str, "size_bytes": int}
        """
        result: BackupResult = {
            "success": False,
            "backup_path": None,
            "size_bytes": 0,
            "backup_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }

        try:
            # 确保备份目录存在
            self._backup_path.mkdir(parents=True, exist_ok=True)

            # 生成备份文件名
            if not backup_name:
                backup_name = f"gold_prices_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d_%H%M%S')}"

            # 检查数据库类型
            if "sqlite" in settings.database_url:
                # SQLite 直接复制文件
                db_path = settings.database_url.replace("sqlite:///", "")
                backup_file = self._backup_path / f"{backup_name}.db"

                if os.path.exists(db_path):
                    shutil.copy2(db_path, backup_file)
                    result["success"] = True
                    result["backup_path"] = str(backup_file)
                    size_bytes = os.path.getsize(backup_file)
                    result["size_bytes"] = size_bytes
                    logger.info(
                        "数据库备份完成: %s (%.2f MB)",
                        backup_file,
                        size_bytes / 1024 / 1024,
                    )
                else:
                    result["error"] = f"数据库文件不存在: {db_path}"
            else:
                # 其他数据库导出为 JSON
                json_data = await self.export_json(limit=100000)
                backup_file = self._backup_path / f"{backup_name}.json"

                with open(backup_file, "w", encoding="utf-8") as f:
                    f.write(json_data)

                result["success"] = True
                result["backup_path"] = str(backup_file)
                result["size_bytes"] = os.path.getsize(backup_file)
                logger.info("数据库备份完成（JSON）: %s", backup_file)

        except Exception as e:
            logger.error("数据库备份失败: %s", e)
            result["error"] = str(e)

        return result

    async def list_backups(self) -> list[BackupInfo]:
        """列出所有备份文件"""
        backups: list[BackupInfo] = []

        try:
            if not self._backup_path.exists():
                return backups

            for f in self._backup_path.iterdir():
                if f.is_file() and f.suffix in (".db", ".json"):
                    stat = f.stat()
                    backups.append(
                        {
                            "name": f.name,
                            "path": str(f),
                            "size_bytes": stat.st_size,
                            "created_at": datetime.fromtimestamp(
                                stat.st_ctime
                            ).isoformat(),
                        }
                    )

            backups.sort(key=lambda x: x["created_at"], reverse=True)

        except Exception as e:
            logger.error("列出备份失败: %s", e)

        return backups

    async def restore_backup(self, backup_path: str) -> dict:
        """从备份恢复数据库

        注意：这是一个危险操作，会覆盖现有数据
        """
        result = {
            "success": False,
            "restored_from": backup_path,
            "restore_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }

        try:
            backup_file = Path(backup_path)
            if not backup_file.exists():
                result["error"] = f"备份文件不存在: {backup_path}"
                return result

            if "sqlite" in settings.database_url and backup_file.suffix == ".db":
                # SQLite 直接替换文件
                db_path = settings.database_url.replace("sqlite:///", "")

                # 先备份当前数据库
                if os.path.exists(db_path):
                    current_backup = f"{db_path}.bak"
                    shutil.copy2(db_path, current_backup)

                # 恢复备份
                shutil.copy2(backup_file, db_path)
                result["success"] = True
                logger.info("数据库恢复完成: %s", backup_path)
            else:
                result["error"] = "暂不支持从此格式恢复"

        except Exception as e:
            logger.error("数据库恢复失败: %s", e)
            result["error"] = str(e)

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
    """启动定时清理任务"""
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
    """获取全局生命周期管理器"""
    global _lifecycle_manager
    if _lifecycle_manager is None and database:
        _lifecycle_manager = DataLifecycleManager(database)
    return _lifecycle_manager


def set_lifecycle_manager(manager: DataLifecycleManager):
    """设置全局生命周期管理器"""
    global _lifecycle_manager
    _lifecycle_manager = manager
