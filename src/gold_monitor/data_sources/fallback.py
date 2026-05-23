"""故障自动切换数据源"""

import logging
from .base import BaseDataSource, PriceData

logger = logging.getLogger(__name__)


class FallbackDataSource(BaseDataSource):
    """支持故障自动切换的数据源

    按优先级尝试多个数据源，当主数据源失败时自动切换到备用源。
    """

    def __init__(self, sources: list[BaseDataSource], max_retries: int = 2):
        if not sources:
            raise ValueError("至少需要一个数据源")
        self._sources = sources
        self._max_retries = max_retries
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

        # 从当前活跃源开始，依次尝试所有源
        for attempt in range(len(self._sources)):
            idx = (self._current_index + attempt) % len(self._sources)
            source = self._sources[idx]

            for retry in range(self._max_retries):
                try:
                    data = await source.fetch_price()
                    # 成功 — 重置失败计数，更新活跃源
                    self._failure_counts[source.name] = 0
                    if idx != self._current_index:
                        logger.warning(
                            "数据源切换: %s -> %s",
                            self._sources[self._current_index].name,
                            source.name
                        )
                        self._current_index = idx
                    return data
                except Exception as e:
                    last_error = e
                    self._failure_counts[source.name] = (
                        self._failure_counts.get(source.name, 0) + 1
                    )
                    logger.warning(
                        "数据源 %s 第 %d 次请求失败: %s",
                        source.name, retry + 1, e
                    )

        # 所有源都失败
        raise ConnectionError(
            f"所有数据源均不可用，最后错误: {last_error}"
        )

    async def health_check(self) -> bool:
        """检查是否至少有一个数据源可用"""
        for source in self._sources:
            if await source.health_check():
                return True
        return False

    def get_status(self) -> dict:
        """返回各数据源的健康状态"""
        return {
            "active": self._sources[self._current_index].name,
            "sources": [
                {
                    "name": s.name,
                    "failures": self._failure_counts.get(s.name, 0)
                }
                for s in self._sources
            ]
        }
