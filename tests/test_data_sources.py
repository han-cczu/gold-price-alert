"""数据源测试"""

import pytest
import asyncio
from gold_monitor.data_sources.mock import MockDataSource
from gold_monitor.data_sources.base import PriceData


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
