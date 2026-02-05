"""数据采集服务"""

import asyncio
import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .models import Database, GoldPrice
from .data_sources.base import BaseDataSource, PriceData
from .data_sources.mock import MockDataSource
from .data_sources.sina import SinaDataSource
from .data_sources.goldapi import GoldAPIDataSource
from .data_sources.fallback import FallbackDataSource

logger = logging.getLogger(__name__)


def create_data_source(source_type: str = None) -> BaseDataSource:
    """创建数据源实例"""
    source_type = source_type or settings.data_source

    if source_type == "mock":
        return MockDataSource()
    elif source_type == "sina":
        return SinaDataSource()
    elif source_type == "goldapi":
        if not settings.goldapi_key:
            raise ValueError("GoldAPI 需要配置 API Key")
        return GoldAPIDataSource(settings.goldapi_key)
    elif source_type == "fallback":
        return create_fallback_source()
    else:
        raise ValueError(f"不支持的数据源类型: {source_type}")


def create_fallback_source() -> FallbackDataSource:
    """创建带故障自动切换的数据源

    按优先级依次添加可用数据源：goldapi > sina > mock
    """
    sources: list[BaseDataSource] = []

    if settings.goldapi_key:
        sources.append(GoldAPIDataSource(settings.goldapi_key))

    sources.append(SinaDataSource())
    sources.append(MockDataSource())

    return FallbackDataSource(sources)


class DataCollector:
    """数据采集器 — 支持断线重连"""

    def __init__(
        self,
        database: Database,
        data_source: BaseDataSource = None,
        on_price_update: Callable[[PriceData], None] = None
    ):
        self._db = database
        self._source = data_source or create_data_source()
        self._scheduler = AsyncIOScheduler()
        self._on_price_update = on_price_update
        self._running = False
        self._last_price: PriceData | None = None

        # 断线重连参数
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._reconnect_backoff = [5, 10, 30, 60, 120]  # 重试退避秒数

    @property
    def last_price(self) -> PriceData | None:
        return self._last_price

    @property
    def is_running(self) -> bool:
        return self._running

    async def fetch_once(self) -> PriceData:
        """执行一次数据采集，带重试"""
        try:
            price_data = await self._source.fetch_price()
            self._last_price = price_data
            self._consecutive_failures = 0

            # 保存到数据库
            self._db.save_price(price_data.price, price_data.source)

            # 触发回调
            if self._on_price_update:
                self._on_price_update(price_data)

            return price_data

        except Exception as e:
            self._consecutive_failures += 1
            logger.error(
                "数据采集失败 (%d/%d): %s",
                self._consecutive_failures,
                self._max_consecutive_failures,
                e
            )

            if self._consecutive_failures >= self._max_consecutive_failures:
                logger.warning("连续失败过多，尝试重新连接...")
                await self._reconnect()

            raise

    async def _reconnect(self):
        """断线重连：重置数据源"""
        backoff_idx = min(
            self._consecutive_failures - self._max_consecutive_failures,
            len(self._reconnect_backoff) - 1
        )
        wait_seconds = self._reconnect_backoff[max(0, backoff_idx)]
        logger.info("等待 %d 秒后重连...", wait_seconds)
        await asyncio.sleep(wait_seconds)

        # 重新创建数据源
        try:
            self._source = create_data_source()
            self._consecutive_failures = 0
            logger.info("数据源重连成功")
        except Exception as e:
            logger.error("数据源重连失败: %s", e)

    def _sync_fetch(self):
        """同步包装的采集方法（供调度器使用）"""
        asyncio.create_task(self.fetch_once())

    def start(self, interval: int = None):
        """启动定时采集"""
        interval = interval or settings.fetch_interval

        self._scheduler.add_job(
            self._sync_fetch,
            trigger=IntervalTrigger(seconds=interval),
            id="gold_price_collector",
            replace_existing=True
        )
        self._scheduler.start()
        self._running = True

    def stop(self):
        """停止采集"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._running = False
