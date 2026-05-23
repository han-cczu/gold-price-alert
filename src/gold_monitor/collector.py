"""数据采集服务 - 高级版

特性：
1. 多数据源并行采集 - 同时从多个源获取，取最快/最可靠的
2. 历史数据补全 - 检测数据间隙并自动补采
3. 采集任务可配置 - 支持运行时修改采集间隔
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from enum import Enum

from .config import settings
from .models import Database
from .data_sources.base import BaseDataSource, PriceData
from .data_sources.mock import MockDataSource
from .data_sources.sina import SinaDataSource
from .data_sources.goldapi import GoldAPIDataSource
from .data_sources.fallback import FallbackDataSource

logger = logging.getLogger(__name__)


# ============ 工具函数 ============

def utcnow() -> datetime:
    """获取当前 UTC 时间（兼容新旧 Python）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    """创建带故障自动切换的数据源"""
    sources: list[BaseDataSource] = []

    if settings.goldapi_key:
        sources.append(GoldAPIDataSource(settings.goldapi_key))

    sources.append(SinaDataSource())
    sources.append(MockDataSource())

    return FallbackDataSource(sources)


def create_all_sources() -> list[BaseDataSource]:
    """创建所有可用数据源（用于并行采集）"""
    sources: list[BaseDataSource] = []

    if settings.goldapi_key:
        sources.append(GoldAPIDataSource(settings.goldapi_key))

    sources.append(SinaDataSource())

    # Mock 作为最后的保底
    if not sources:
        sources.append(MockDataSource())

    return sources


# ============ 采集策略 ============

class FetchStrategy(Enum):
    """采集策略"""
    SINGLE = "single"           # 单数据源
    FALLBACK = "fallback"       # 顺序降级
    PARALLEL_FIRST = "parallel_first"   # 并行取最快
    PARALLEL_VOTE = "parallel_vote"     # 并行投票（取中位数）


# ============ 统计信息 ============

@dataclass
class SourceStats:
    """单个数据源统计"""
    name: str
    total_fetches: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0
    last_latency_ms: float = 0
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def success_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.success_count / self.total_fetches * 100

    @property
    def avg_latency_ms(self) -> float:
        if self.success_count == 0:
            return 0.0
        return self.total_latency_ms / self.success_count

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_fetches": self.total_fetches,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_latency_ms": round(self.last_latency_ms, 2),
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error
        }


@dataclass
class SourceQuality:
    """数据源质量评分"""
    name: str
    reliability_score: float  # 可靠性评分 (0-100)
    freshness_score: float    # 数据新鲜度 (0-100)
    latency_score: float      # 延迟评分 (0-100)
    overall_score: float      # 综合评分 (0-100)
    weight: float             # 融合权重 (0-1)
    status: str               # healthy/degraded/unhealthy

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "reliability_score": round(self.reliability_score, 1),
            "freshness_score": round(self.freshness_score, 1),
            "latency_score": round(self.latency_score, 1),
            "overall_score": round(self.overall_score, 1),
            "weight": round(self.weight, 3),
            "status": self.status
        }


def calculate_source_quality(stats: SourceStats) -> SourceQuality:
    """计算数据源质量评分"""
    # 可靠性评分（基于成功率）
    reliability_score = stats.success_rate

    # 新鲜度评分（基于最后成功时间）
    freshness_score = 100.0
    if stats.last_success_at:
        age_seconds = (utcnow() - stats.last_success_at).total_seconds()
        # 1分钟内满分，5分钟后开始衰减
        if age_seconds > 60:
            freshness_score = max(0, 100 - (age_seconds - 60) / 6)  # 每6秒减1分

    # 延迟评分（基于平均延迟）
    latency_score = 100.0
    if stats.avg_latency_ms > 0:
        # 500ms 以内满分，超过 5000ms 为 0 分
        latency_score = max(0, 100 - (stats.avg_latency_ms - 500) / 45)

    # 综合评分（加权平均）
    overall_score = (
        reliability_score * 0.5 +
        freshness_score * 0.3 +
        latency_score * 0.2
    )

    # 计算融合权重
    weight = overall_score / 100.0

    # 确定状态
    if overall_score >= 80:
        status = "healthy"
    elif overall_score >= 50:
        status = "degraded"
    else:
        status = "unhealthy"

    return SourceQuality(
        name=stats.name,
        reliability_score=reliability_score,
        freshness_score=freshness_score,
        latency_score=latency_score,
        overall_score=overall_score,
        weight=weight,
        status=status
    )


@dataclass
class CollectorStats:
    """采集器统计信息"""
    started_at: datetime = field(default_factory=utcnow)
    total_fetches: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    gaps_detected: int = 0
    gaps_filled: int = 0
    source_stats: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.success_count / self.total_fetches * 100

    @property
    def uptime_seconds(self) -> float:
        return (utcnow() - self.started_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": round(self.uptime_seconds, 2),
            "total_fetches": self.total_fetches,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 2),
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "last_error": self.last_error,
            "gaps_detected": self.gaps_detected,
            "gaps_filled": self.gaps_filled,
            "source_stats": {k: v.to_dict() for k, v in self.source_stats.items()}
        }


# ============ 高级采集器 ============

class AdvancedCollector:
    """高级金价采集器
    
    特性：
    1. 多数据源并行采集 - 同时从多个源获取，取最快/最可靠的
    2. 历史数据补全 - 检测数据间隙并自动补采
    3. 采集任务可配置 - 支持运行时修改采集间隔
    """

    def __init__(
        self,
        database: Database,
        strategy: FetchStrategy = FetchStrategy.PARALLEL_FIRST,
        on_price_update: Callable[[PriceData], None] = None,
        deduplicate: bool = True,
        dedupe_threshold: float = 0.01,
        gap_detection: bool = True,
        gap_threshold_minutes: int = 5  # 超过此间隔视为数据间隙
    ):
        self._db = database
        self._strategy = strategy
        self._on_price_update = on_price_update
        self._deduplicate = deduplicate
        self._dedupe_threshold = dedupe_threshold
        self._gap_detection = gap_detection
        self._gap_threshold = timedelta(minutes=gap_threshold_minutes)

        # 数据源管理
        self._sources: list[BaseDataSource] = []
        self._source_lock = asyncio.Lock()

        # 运行状态
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._interval = settings.fetch_interval
        self._interval_changed = asyncio.Event()

        # 数据状态
        self._last_price: Optional[PriceData] = None
        self._stats = CollectorStats()

        # 重连配置
        self._max_consecutive_failures = 5
        self._backoff_base = 2
        self._backoff_max = 300

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

    def set_interval(self, seconds: int):
        """运行时修改采集间隔"""
        if seconds < 1:
            raise ValueError("采集间隔不能小于 1 秒")
        
        old_interval = self._interval
        self._interval = seconds
        self._interval_changed.set()
        logger.info("采集间隔已修改: %d秒 -> %d秒", old_interval, seconds)

    def set_strategy(self, strategy: FetchStrategy):
        """运行时修改采集策略"""
        old_strategy = self._strategy
        self._strategy = strategy
        logger.info("采集策略已修改: %s -> %s", old_strategy.value, strategy.value)

    def get_config(self) -> dict:
        """获取当前配置"""
        return {
            "interval": self._interval,
            "strategy": self._strategy.value,
            "deduplicate": self._deduplicate,
            "dedupe_threshold": self._dedupe_threshold,
            "gap_detection": self._gap_detection,
            "gap_threshold_minutes": self._gap_threshold.total_seconds() / 60,
            "sources": [s.name for s in self._sources] if self._sources else []
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
            return {
                "confidence": 0,
                "status": "unknown",
                "message": "无数据源"
            }

        # 使用最佳数据源的评分作为置信度
        best = qualities[0]
        
        # 计算加权平均置信度（如果有多个健康数据源）
        healthy_sources = [q for q in qualities if q.status == "healthy"]
        if len(healthy_sources) > 1:
            weighted_sum = sum(q.overall_score * q.weight for q in healthy_sources)
            total_weight = sum(q.weight for q in healthy_sources)
            confidence = weighted_sum / total_weight if total_weight > 0 else best.overall_score
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
            "total_sources": len(qualities)
        }

    # ============ 数据源管理 ============

    async def _init_sources(self):
        """初始化数据源"""
        async with self._source_lock:
            if self._sources:
                return

            if self._strategy in (FetchStrategy.PARALLEL_FIRST, FetchStrategy.PARALLEL_VOTE):
                self._sources = create_all_sources()
            elif self._strategy == FetchStrategy.FALLBACK:
                self._sources = [create_fallback_source()]
            else:
                self._sources = [create_data_source()]

            # 初始化每个数据源的统计
            for source in self._sources:
                self._stats.source_stats[source.name] = SourceStats(name=source.name)

            logger.info(
                "初始化 %d 个数据源: %s",
                len(self._sources),
                [s.name for s in self._sources]
            )

    async def _close_sources(self):
        """关闭所有数据源"""
        async with self._source_lock:
            for source in self._sources:
                if hasattr(source, 'close'):
                    try:
                        await source.close()
                    except Exception as e:
                        logger.warning("关闭数据源 %s 失败: %s", source.name, e)
            self._sources = []

    # ============ 并行采集 ============

    async def _fetch_from_source(self, source: BaseDataSource) -> tuple[str, Optional[PriceData], float]:
        """从单个数据源获取价格，返回 (源名称, 价格数据, 延迟ms)"""
        source_stats = self._stats.source_stats.get(source.name)
        if source_stats:
            source_stats.total_fetches += 1

        start_time = asyncio.get_event_loop().time()

        try:
            price_data = await asyncio.wait_for(
                source.fetch_price(),
                timeout=10.0  # 10秒超时
            )
            latency = (asyncio.get_event_loop().time() - start_time) * 1000

            if source_stats:
                source_stats.success_count += 1
                source_stats.last_latency_ms = latency
                source_stats.total_latency_ms += latency
                source_stats.last_success_at = utcnow()

            return (source.name, price_data, latency)

        except asyncio.TimeoutError:
            if source_stats:
                source_stats.failure_count += 1
                source_stats.last_error = "超时"
            return (source.name, None, 0)

        except Exception as e:
            if source_stats:
                source_stats.failure_count += 1
                source_stats.last_error = str(e)
            return (source.name, None, 0)

    async def _fetch_parallel_first(self) -> Optional[PriceData]:
        """并行采集，取最快返回的有效结果

        关键修复：最快完成的源若失败（返回 None），继续等待其它仍在运行的源，
        而不是直接放弃。只有在拿到有效结果或全部完成后才取消剩余任务。
        """
        if not self._sources:
            await self._init_sources()

        tasks = {
            asyncio.create_task(self._fetch_from_source(source))
            for source in self._sources
        }

        result = None
        pending = set(tasks)
        try:
            while pending and result is None:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        name, price_data, latency = task.result()
                    except asyncio.CancelledError:
                        continue
                    if price_data:
                        result = price_data
                        logger.info(
                            "并行采集: %s 返回 (%.1fms), 价格: $%.2f",
                            name, latency, price_data.price
                        )
                        break
        finally:
            # 取消尚未完成的任务并回收，避免悬挂任务告警
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        return result

    async def _fetch_parallel_vote(self) -> Optional[PriceData]:
        """并行采集，取所有结果的中位数"""
        if not self._sources:
            await self._init_sources()

        tasks = [
            asyncio.create_task(self._fetch_from_source(source))
            for source in self._sources
        ]

        # 等待所有任务完成
        results = await asyncio.gather(*tasks, return_exceptions=True)

        prices = []
        for result in results:
            if isinstance(result, tuple):
                name, price_data, latency = result
                if price_data:
                    prices.append((price_data, latency))
                    logger.debug("投票采集: %s 返回 $%.2f", name, price_data.price)

        if not prices:
            return None

        # 取中位数
        prices.sort(key=lambda x: x[0].price)
        median_idx = len(prices) // 2
        median_price_data, _ = prices[median_idx]

        logger.info(
            "投票采集: %d/%d 成功, 中位数价格: $%.2f",
            len(prices), len(self._sources), median_price_data.price
        )

        return median_price_data

    # ============ 数据采集 ============

    async def fetch_once(self) -> Optional[PriceData]:
        """执行一次数据采集"""
        self._stats.total_fetches += 1

        try:
            # 根据策略选择采集方式
            if self._strategy == FetchStrategy.PARALLEL_FIRST:
                price_data = await self._fetch_parallel_first()
            elif self._strategy == FetchStrategy.PARALLEL_VOTE:
                price_data = await self._fetch_parallel_vote()
            else:
                # 单数据源或 Fallback
                if not self._sources:
                    await self._init_sources()
                _, price_data, _ = await self._fetch_from_source(self._sources[0])

            if not price_data:
                raise Exception("所有数据源均获取失败")

            # 更新统计
            self._stats.success_count += 1
            self._stats.last_success_at = utcnow()
            self._stats.consecutive_failures = 0

            # 去重判断
            if self._should_save(price_data.price):
                self._db.save_price(price_data.price, price_data.source)
                logger.info(
                    "采集金价: $%.2f (来源: %s, 策略: %s)",
                    price_data.price, price_data.source, self._strategy.value
                )
            else:
                logger.debug("价格无变化，跳过保存: $%.2f", price_data.price)

            self._last_price = price_data

            # 触发回调
            if self._on_price_update:
                try:
                    result = self._on_price_update(price_data)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error("价格更新回调失败: %s", e)

            return price_data

        except Exception as e:
            self._stats.failure_count += 1
            self._stats.last_failure_at = utcnow()
            self._stats.consecutive_failures += 1
            self._stats.last_error = str(e)

            logger.error(
                "数据采集失败 (%d 次连续失败): %s",
                self._stats.consecutive_failures, e
            )

            return None

    def _should_save(self, new_price: float) -> bool:
        """判断是否应该保存（去重逻辑）"""
        if not self._deduplicate:
            return True

        if self._last_price is None:
            return True

        price_diff = abs(new_price - self._last_price.price)
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

        # 获取历史数据
        records = self._db.get_prices_in_range(start_time, end_time)
        if len(records) < 2:
            return []

        gaps = []
        for i in range(1, len(records)):
            prev_time = records[i - 1].timestamp
            curr_time = records[i].timestamp
            gap = curr_time - prev_time

            if gap > self._gap_threshold:
                gaps.append((prev_time, curr_time))
                self._stats.gaps_detected += 1

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
            len(gaps)
        )
        price_data = await self.fetch_once()
        recorded = 1 if price_data else 0
        self._stats.gaps_filled += recorded
        return recorded

    # ============ 采集循环 ============

    async def _collect_loop(self):
        """主采集循环"""
        logger.info(
            "数据采集启动 (间隔: %d秒, 策略: %s, 去重: %s, 间隙检测: %s)",
            self._interval, self._strategy.value, self._deduplicate, self._gap_detection
        )

        # 启动时立即采集一次
        await self.fetch_once()

        # 启动时检测并填补间隙
        if self._gap_detection:
            asyncio.create_task(self.fill_gaps(hours=1))

        while self._running:
            # 计算等待时间
            if self._stats.consecutive_failures > 0:
                wait_time = min(
                    self._backoff_base ** self._stats.consecutive_failures,
                    self._backoff_max
                )
                logger.info("等待 %.1f 秒后重试...", wait_time)
            else:
                wait_time = self._interval

            # 等待，但响应间隔修改
            try:
                await asyncio.wait_for(
                    self._interval_changed.wait(),
                    timeout=wait_time
                )
                self._interval_changed.clear()
                logger.debug("检测到间隔修改，立即执行下一次采集")
            except asyncio.TimeoutError:
                pass

            if self._running:
                await self.fetch_once()

    # ============ 启停控制 ============

    def start(self, interval: int = None):
        """启动定时采集"""
        if self._running:
            logger.warning("采集器已在运行")
            return

        if interval:
            self._interval = interval

        self._running = True
        self._stats = CollectorStats()
        self._task = asyncio.create_task(self._collect_loop())

    async def stop(self):
        """停止采集"""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self._close_sources()
        logger.info("数据采集已停止")

    async def restart(self, interval: int = None):
        """重启采集器"""
        await self.stop()
        self.start(interval)


# ============ 兼容旧版 API ============

# 别名，保持向后兼容
PriceCollector = AdvancedCollector

# 全局采集器实例
_collector: Optional[AdvancedCollector] = None


def get_collector() -> Optional[AdvancedCollector]:
    """获取全局采集器实例"""
    return _collector


def set_collector(collector: AdvancedCollector):
    """设置全局采集器实例"""
    global _collector
    _collector = collector
