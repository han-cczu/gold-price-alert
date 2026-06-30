"""模拟数据源 - 用于开发测试"""

import random
from datetime import datetime, timezone
from .base import BaseDataSource, PriceData


class MockDataSource(BaseDataSource):
    """模拟金价数据源"""

    def __init__(self, base_price: float = 2050.0, volatility: float = 0.5):
        """
        初始化模拟数据源

        Args:
            base_price: 基准价格 (USD/oz)
            volatility: 波动率（百分比）
        """
        self._base_price = base_price
        self._volatility = volatility
        self._current_price = base_price

    @property
    def name(self) -> str:
        return "mock"

    async def fetch_price(self) -> PriceData:
        """生成模拟金价数据"""
        # 模拟价格随机波动
        change_percent = random.uniform(-self._volatility, self._volatility)
        self._current_price *= 1 + change_percent / 100

        # 限制价格在合理范围内
        self._current_price = max(1800, min(2500, self._current_price))

        return PriceData(
            price=round(self._current_price, 2),
            currency="USD",
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            source=self.name,
        )
