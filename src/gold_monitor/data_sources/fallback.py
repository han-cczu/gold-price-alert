"""故障自动切换数据源"""

import asyncio
import logging
from math import isfinite
from .base import BaseDataSource, PriceData, close_data_source

logger = logging.getLogger(__name__)


class FallbackDataSource(BaseDataSource):
    """支持故障自动切换的数据源

    按优先级尝试多个数据源，当主数据源失败时自动切换到备用源。
    """

    def __init__(
        self,
        sources: list[BaseDataSource],
        max_retries: int = 2,
        *,
        source_timeout: float = 10.0,
    ):
        if not sources:
            raise ValueError("至少需要一个数据源")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 1
        ):
            raise ValueError("重试次数不能小于 1")
        if (
            isinstance(source_timeout, bool)
            or not isfinite(source_timeout)
            or source_timeout <= 0
        ):
            raise ValueError("数据源超时必须为正有限秒数")
        self._sources = sources
        self._max_retries = max_retries
        self._source_timeout = source_timeout
        self._current_index = 0
        self._failure_counts: dict[str, int] = {}

    @property
    def name(self) -> str:
        return f"fallback({self._sources[self._current_index].name})"

    @property
    def active_source(self) -> BaseDataSource:
        return self._sources[self._current_index]

    async def fetch_price(self) -> PriceData:
        """按优先级尝试各数据源获取金价"""
        last_error = None

        # Recheck priority on every round so a recovered primary is used again.
        # Retries share one source budget; a hung primary cannot consume the
        # backup's budget. Cancellation propagates instead of triggering failover.
        for idx, source in enumerate(self._sources):
            deadline = asyncio.get_running_loop().time() + self._source_timeout
            for retry in range(self._max_retries):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(
                        source.fetch_price(), timeout=remaining
                    )
                    if not isfinite(data.price) or data.price <= 0:
                        raise ValueError("数据源返回了无效价格")
                    # 成功 — 重置失败计数，更新活跃源
                    self._failure_counts[source.name] = 0
                    if idx != self._current_index:
                        logger.warning(
                            "数据源切换: %s -> %s",
                            self._sources[self._current_index].name,
                            source.name,
                        )
                        self._current_index = idx
                    return data
                except Exception as e:
                    last_error = e
                    self._failure_counts[source.name] = (
                        self._failure_counts.get(source.name, 0) + 1
                    )
                    logger.warning(
                        "数据源 %s 第 %d 次请求失败: %s", source.name, retry + 1, e
                    )
                    if isinstance(e, asyncio.TimeoutError):
                        break

        # 所有源都失败
        raise ConnectionError(f"所有数据源均不可用，最后错误: {last_error}")

    async def health_check(self) -> bool:
        """检查是否至少有一个数据源可用"""
        try:
            await self.fetch_price()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """The wrapper owns every child, including inactive fallback sources."""
        for source in self._sources:
            try:
                await close_data_source(source)
            except Exception:
                logger.exception("关闭备用数据源 %s 失败", source.name)

    def get_status(self) -> dict:
        """返回各数据源的健康状态"""
        return {
            "active": self._sources[self._current_index].name,
            "sources": [
                {"name": s.name, "failures": self._failure_counts.get(s.name, 0)}
                for s in self._sources
            ],
        }
