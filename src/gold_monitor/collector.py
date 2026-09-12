"""编排当前报价的获取、去重入库和回调。

来源创建、采集策略、统计与调度分别由独立模块负责。检测到历史间隙时
只能补充一个当前样本，不能恢复历史报价。
"""

import asyncio
from collections.abc import Awaitable
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Callable, Optional

from .config import Settings, settings
from .metrics import record_fetch
from .models import Database
from inspect import isawaitable

from .data_sources.base import BaseDataSource, PriceData, close_data_source
from .data_sources.fallback import FallbackDataSource
from .data_sources.factory import (
    create_data_source as create_data_source,
    create_all_sources as create_all_sources,
    create_fallback_source as create_fallback_source,
)
from .collection.stats import (
    CollectorStats as CollectorStats,
    SourceStats as SourceStats,
    SourceQuality as SourceQuality,
    calculate_source_quality as calculate_source_quality,
    utcnow as utcnow,
)
from .collection.strategies import (
    FetchStrategy as FetchStrategy,
    parallel_first,
    parallel_vote,
)
from .collection.scheduler import CollectionScheduler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one attempt; receiving a quote does not imply a database insert."""

    price: PriceData | None
    saved: bool = False
    error: str | None = None


# ============ 高级采集器 ============


class AdvancedCollector:
    """应用实例的采集服务；保留原名称与 fetch_once 返回值兼容。"""

    def __init__(
        self,
        database: Database,
        strategy: FetchStrategy = FetchStrategy.PARALLEL_FIRST,
        on_price_update: Callable[[PriceData], Awaitable[None] | None] | None = None,
        deduplicate: bool = True,
        dedupe_threshold: float = 0.01,
        gap_detection: bool = True,
        gap_threshold_minutes: int = 5,  # 超过此间隔视为数据间隙
        config: Settings | None = None,
        dedupe_max_interval_seconds: int | None = None,
    ):
        self._config = config if config is not None else settings
        if dedupe_threshold < 0 or not isfinite(dedupe_threshold):
            raise ValueError("去重阈值必须为非负有限数")
        if gap_threshold_minutes < 1:
            raise ValueError("间隙阈值不能小于 1 分钟")
        self._db = database
        self._strategy = FetchStrategy(strategy)
        self._on_price_update = on_price_update
        self._deduplicate = deduplicate
        self._dedupe_threshold = dedupe_threshold
        heartbeat = (
            self._config.price_heartbeat_seconds
            if dedupe_max_interval_seconds is None
            else dedupe_max_interval_seconds
        )
        self._validate_interval(heartbeat)
        self._dedupe_max_interval = timedelta(seconds=heartbeat)
        self._gap_detection = gap_detection
        self._gap_threshold = timedelta(minutes=gap_threshold_minutes)

        # 数据源管理
        self._sources: list[BaseDataSource] = []
        self._source_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

        # 运行状态
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._interval = self._validate_interval(self._config.fetch_interval)
        self._interval_changed = asyncio.Event()

        # 数据状态
        self._last_price: Optional[PriceData] = None
        self._last_saved_price: Optional[PriceData] = None
        self._stats = CollectorStats()

        self._scheduler = CollectionScheduler(
            collect=self.fetch_once,
            recover=(lambda: self.fill_gaps(hours=1)) if gap_detection else None,
            interval=lambda: self._interval,
            failures=lambda: self._stats.consecutive_failures,
            interval_changed=self._interval_changed,
        )

    # ============ 属性 ============

    @property
    def last_price(self) -> Optional[PriceData]:
        return self._last_price

    @property
    def stats(self) -> CollectorStats:
        return self._stats

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def strategy(self) -> FetchStrategy:
        return self._strategy

    # ============ 运行时配置 ============

    @staticmethod
    def _validate_interval(seconds: int) -> int:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= 3600
        ):
            raise ValueError("采集间隔必须在 1 到 3600 秒之间")
        return seconds

    def set_interval(self, seconds: int):
        """运行时修改采集间隔"""
        self._validate_interval(seconds)

        old_interval = self._interval
        self._interval = seconds
        self._interval_changed.set()
        logger.info("采集间隔已修改: %d秒 -> %d秒", old_interval, seconds)

    def set_strategy(self, strategy: FetchStrategy):
        """Compatibility wrapper; await its task when resources already exist."""
        strategy = FetchStrategy(strategy)
        if not self._sources:
            self._strategy = strategy
            return None
        task = asyncio.create_task(self.reconfigure(strategy=strategy))
        self._track_task(task)
        return task

    async def reconfigure(
        self,
        *,
        interval: int | None = None,
        strategy: FetchStrategy | str | None = None,
    ) -> dict:
        """Validate the whole request before changing settings or source ownership."""
        if interval is not None:
            self._validate_interval(interval)
        target = FetchStrategy(strategy) if strategy is not None else None
        async with self._operation_lock:
            next_strategy = target if target is not None else self._strategy
            replacement = (
                self._make_sources(next_strategy)
                if next_strategy != self._strategy
                else None
            )
            old_sources = self._sources
            if replacement is not None:
                self._sources = replacement
                self._strategy = next_strategy
                self._stats.source_stats = {
                    source.name: SourceStats(name=source.name) for source in replacement
                }
            if interval is not None:
                self.set_interval(interval)
            if replacement is not None:
                await self._close_source_list(old_sources)
        return self.get_config()

    def _make_sources(self, strategy: FetchStrategy) -> list[BaseDataSource]:
        if strategy in (FetchStrategy.PARALLEL_FIRST, FetchStrategy.PARALLEL_VOTE):
            return create_all_sources(self._config)
        if strategy == FetchStrategy.FALLBACK:
            return [create_fallback_source(self._config)]
        return [create_data_source(config=self._config)]

    def _track_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("采集后台任务失败: %s", done.exception())

        task.add_done_callback(finished)

    def get_config(self) -> dict:
        """获取当前配置"""
        return {
            "interval": self._interval,
            "strategy": self._strategy.value,
            "deduplicate": self._deduplicate,
            "dedupe_threshold": self._dedupe_threshold,
            "dedupe_max_interval_seconds": int(
                self._dedupe_max_interval.total_seconds()
            ),
            "gap_detection": self._gap_detection,
            "gap_threshold_minutes": self._gap_threshold.total_seconds() / 60,
            "sources": [s.name for s in self._sources] if self._sources else [],
        }

    def get_source_quality(self) -> list[SourceQuality]:
        """获取所有数据源的质量评分"""
        qualities = []
        for name, stats in self._stats.source_stats.items():
            quality = calculate_source_quality(stats)
            qualities.append(quality)

        # 按综合评分排序
        qualities.sort(key=lambda q: q.overall_score, reverse=True)
        return qualities

    def get_overall_confidence(self) -> dict:
        """获取整体数据置信度"""
        qualities = self.get_source_quality()
        if not qualities:
            return {"confidence": 0, "status": "unknown", "message": "无数据源"}

        # 使用最佳数据源的评分作为置信度
        best = qualities[0]

        # 计算加权平均置信度（如果有多个健康数据源）
        healthy_sources = [q for q in qualities if q.status == "healthy"]
        if len(healthy_sources) > 1:
            weighted_sum = sum(q.overall_score * q.weight for q in healthy_sources)
            total_weight = sum(q.weight for q in healthy_sources)
            confidence = (
                weighted_sum / total_weight if total_weight > 0 else best.overall_score
            )
        else:
            confidence = best.overall_score

        # 确定状态和消息
        if confidence >= 80:
            status = "high"
            message = "数据可信度高"
        elif confidence >= 60:
            status = "medium"
            message = "数据可信度中等"
        elif confidence >= 40:
            status = "low"
            message = "数据可信度较低，请谨慎参考"
        else:
            status = "very_low"
            message = "数据可信度很低，可能存在问题"

        return {
            "confidence": round(confidence, 1),
            "status": status,
            "message": message,
            "best_source": best.name,
            "healthy_sources": len(healthy_sources),
            "total_sources": len(qualities),
        }

    # ============ 数据源管理 ============

    async def _init_sources(self):
        """初始化数据源"""
        async with self._source_lock:
            if self._sources:
                return

            self._sources = self._make_sources(self._strategy)

            # 初始化每个数据源的统计
            for source in self._sources:
                self._stats.source_stats[source.name] = SourceStats(name=source.name)

            logger.info(
                "初始化 %d 个数据源: %s",
                len(self._sources),
                [s.name for s in self._sources],
            )

    async def _close_sources(self):
        """关闭所有数据源"""
        async with self._source_lock:
            await self._close_source_list(self._sources)
            self._sources = []

    @staticmethod
    async def _close_source_list(sources: list[BaseDataSource]) -> None:
        for source in sources:
            try:
                await close_data_source(source)
            except Exception as e:
                logger.warning("关闭数据源 %s 失败: %s", source.name, e)

    # ============ 并行采集 ============

    async def _fetch_from_source(
        self, source: BaseDataSource
    ) -> tuple[str, Optional[PriceData], float]:
        """从单个数据源获取价格，返回 (源名称, 价格数据, 延迟ms)"""
        source_stats = self._stats.source_stats.setdefault(
            source.name, SourceStats(name=source.name)
        )
        if source_stats:
            source_stats.total_fetches += 1

        start_time = asyncio.get_event_loop().time()

        try:
            if isinstance(source, FallbackDataSource):
                # Fallback owns a separate bounded budget for each real source.
                price_data = await source.fetch_price()
            else:
                price_data = await asyncio.wait_for(source.fetch_price(), timeout=10.0)
            latency = (asyncio.get_event_loop().time() - start_time) * 1000

            if not isfinite(price_data.price) or price_data.price <= 0:
                raise ValueError("数据源返回了无效价格")

            if source_stats:
                source_stats.success_count += 1
                source_stats.last_latency_ms = latency
                source_stats.total_latency_ms += latency
                source_stats.last_success_at = utcnow()
            record_fetch(source.name, True, latency / 1000)

            return (source.name, price_data, latency)

        except asyncio.TimeoutError:
            if source_stats:
                source_stats.failure_count += 1
                source_stats.last_error = "超时"
            record_fetch(source.name, False, 0.0)
            return (source.name, None, 0)

        except Exception as e:
            if source_stats:
                source_stats.failure_count += 1
                source_stats.last_error = str(e)
            record_fetch(source.name, False, 0.0)
            return (source.name, None, 0)

    async def _fetch_parallel_first(self) -> Optional[PriceData]:
        if not self._sources:
            await self._init_sources()
        return await parallel_first(self._sources, self._fetch_from_source)

    async def _fetch_parallel_vote(self) -> Optional[PriceData]:
        if not self._sources:
            await self._init_sources()
        return await parallel_vote(self._sources, self._fetch_from_source)

    # ============ 数据采集 ============

    async def fetch_once(self) -> Optional[PriceData]:
        """Compatibility API returning the received quote, including deduplicated ones."""
        return (await self.collect()).price

    async def collect(self) -> FetchResult:
        """Serialize source use and persistence; callbacks receive every valid quote."""
        async with self._operation_lock:
            result = await self._collect_locked()
            if result.price is not None:
                result.price.recorded = result.saved
        if result.price is not None and self._on_price_update:
            try:
                callback = self._on_price_update(result.price)
                if isawaitable(callback):
                    await callback
            except Exception:
                logger.exception("价格更新回调失败")
        return result

    async def _collect_locked(self) -> FetchResult:
        self._stats.total_fetches += 1
        try:
            if self._strategy == FetchStrategy.PARALLEL_FIRST:
                price_data = await self._fetch_parallel_first()
            elif self._strategy == FetchStrategy.PARALLEL_VOTE:
                price_data = await self._fetch_parallel_vote()
            else:
                if not self._sources:
                    await self._init_sources()
                _, price_data, _ = await self._fetch_from_source(self._sources[0])
            if price_data is None:
                raise ConnectionError("所有数据源均获取失败")

            saved = self._should_save(price_data.price, price_data.timestamp) or (
                self._last_saved_price is not None
                and self._last_saved_price.currency != price_data.currency
            )
            if saved:
                await self._db.run(
                    self._db.save_price,
                    price_data.price,
                    price_data.source,
                    timestamp=price_data.timestamp,
                    currency=price_data.currency,
                )
                self._last_saved_price = price_data
                self._stats.saved_count += 1
            else:
                self._stats.skipped_count += 1
            self._last_price = price_data
            self._stats.success_count += 1
            self._stats.last_success_at = utcnow()
            self._stats.consecutive_failures = 0
            self._stats.last_error = None
            return FetchResult(price_data, saved=saved)
        except Exception as e:
            self._stats.failure_count += 1
            self._stats.last_failure_at = utcnow()
            self._stats.consecutive_failures += 1
            self._stats.last_error = str(e)
            logger.error("数据采集失败: %s", e)
            return FetchResult(None, error=str(e))

    def _should_save(self, new_price: float, timestamp: datetime | None = None) -> bool:
        """Deduplicate price changes while retaining periodic real observations."""
        if not self._deduplicate:
            return True

        if self._last_saved_price is None:
            return True

        previous_time = self._last_saved_price.timestamp
        if (
            timestamp is not None
            and previous_time is not None
            and timestamp - previous_time >= self._dedupe_max_interval
        ):
            return True

        price_diff = abs(new_price - self._last_saved_price.price)
        return price_diff >= self._dedupe_threshold

    # ============ 历史数据补全 ============

    async def detect_gaps(self, hours: int = 24) -> list[tuple[datetime, datetime]]:
        """检测数据间隙

        返回: [(间隙开始时间, 间隙结束时间), ...]
        """
        if not self._gap_detection:
            return []

        end_time = utcnow()
        start_time = end_time - timedelta(hours=hours)

        gaps: list[tuple[datetime, datetime]] = []
        previous: datetime | None = None
        offset, page_size = 0, 1000
        while True:
            records = await self._db.run(
                self._db.get_prices_in_range,
                start_time,
                end_time,
                limit=page_size,
                offset=offset,
            )
            for record in records:
                if (
                    previous is not None
                    and record.timestamp - previous > self._gap_threshold
                ):
                    gaps.append((previous, record.timestamp))
                previous = record.timestamp
            if len(records) < page_size:
                break
            offset += page_size
        self._stats.gaps_detected += len(gaps)

        if gaps:
            logger.info("检测到 %d 个数据间隙", len(gaps))

        return gaps

    async def fill_gaps(self, hours: int = 24) -> int:
        """检测到数据间隙后，采集一个当前样本以接续时间序列。

        重要说明：本系统的数据源只提供"当前"现货价，**无法获取历史时刻的真实价格**，
        因此真正的历史间隙回填是不可能的。早先的实现会对每个间隙各抓一次当前价并以
        当前时间戳入库，结果是把几乎相同的当前价重复写入多次——既不能还原历史，
        反而污染数据。现改为：检测间隙后只采集一个当前样本恢复序列连续性，
        绝不伪造历史数据点。

        返回: 实际新增的样本数（0 或 1）。
        """
        gaps = await self.detect_gaps(hours)
        if not gaps:
            return 0

        logger.info(
            "检测到 %d 处历史数据间隙；现货数据源无法回填历史价格，仅采集一个当前样本以接续序列",
            len(gaps),
        )
        result = await self.collect()
        recorded = int(result.saved)
        self._stats.gaps_filled += recorded
        return recorded

    # ============ 采集循环 ============

    async def _collect_loop(self):
        await self._scheduler.run()

    # ============ 启停控制 ============

    def start(self, interval: int | None = None):
        """启动定时采集"""
        if self._running:
            logger.warning("采集器已在运行")
            return

        if interval is not None:
            self._validate_interval(interval)
            self._interval = interval

        self._running = True
        self._stats = CollectorStats()
        self._task = asyncio.create_task(self._collect_loop())

    async def stop(self):
        """停止采集"""
        self._running = False

        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._operation_lock:
            await self._close_sources()
        self._task = None
        logger.info("数据采集已停止")

    async def restart(self, interval: int | None = None):
        """重启采集器"""
        await self.stop()
        self.start(interval)
