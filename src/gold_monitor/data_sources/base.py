"""数据源基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any


@dataclass
class PriceData:
    """金价数据结构"""

    price: float  # 价格 (USD/oz)
    currency: str = "USD"
    timestamp: datetime | None = None
    source: str = "unknown"

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).replace(tzinfo=None)


class BaseDataSource(ABC):
    """数据源基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称"""
        pass

    @abstractmethod
    async def fetch_price(self) -> PriceData:
        """获取当前金价"""
        pass

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            await self.fetch_price()
            return True
        except Exception:
            return False


async def close_data_source(source: Any) -> None:
    """关闭按需创建的数据源，兼容 async/sync close。"""
    close = getattr(source, "close", None)
    if not callable(close):
        return

    result = close()
    if isawaitable(result):
        await result
