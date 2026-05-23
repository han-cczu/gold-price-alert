"""数据源测试"""

import pytest
from gold_monitor.data_sources.mock import MockDataSource
from gold_monitor.data_sources.base import BaseDataSource, PriceData
from gold_monitor.data_sources.fallback import FallbackDataSource


class _AlwaysFail(BaseDataSource):
    def __init__(self, name="fail"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def fetch_price(self) -> PriceData:
        raise RuntimeError("down")


@pytest.mark.asyncio
async def test_mock_data_source():
    """测试模拟数据源"""
    source = MockDataSource(base_price=2000.0, volatility=0.5)

    assert source.name == "mock"

    price_data = await source.fetch_price()

    assert isinstance(price_data, PriceData)
    assert 1800 <= price_data.price <= 2500
    assert price_data.currency == "USD"
    assert price_data.source == "mock"


@pytest.mark.asyncio
async def test_mock_data_source_volatility():
    """测试模拟数据源波动性"""
    source = MockDataSource(base_price=2000.0, volatility=1.0)

    prices = []
    for _ in range(10):
        price_data = await source.fetch_price()
        prices.append(price_data.price)

    # 确保价格有变化
    assert len(set(prices)) > 1


@pytest.mark.asyncio
async def test_fallback_switches_to_healthy_source():
    """主源失败时自动切换到备用源"""
    fallback = FallbackDataSource([_AlwaysFail("primary"), MockDataSource(base_price=2000.0)])

    data = await fallback.fetch_price()

    assert isinstance(data, PriceData)
    assert data.source == "mock"
    assert fallback.active_source.name == "mock"


@pytest.mark.asyncio
async def test_fallback_raises_when_all_fail():
    """所有源都失败时抛出 ConnectionError"""
    fallback = FallbackDataSource([_AlwaysFail("a"), _AlwaysFail("b")])

    with pytest.raises(ConnectionError):
        await fallback.fetch_price()


def test_fallback_requires_at_least_one_source():
    with pytest.raises(ValueError):
        FallbackDataSource([])
