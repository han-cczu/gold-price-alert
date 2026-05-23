"""采集器测试 - 重点验证 PARALLEL_FIRST 容错回退"""

import asyncio

import pytest

from gold_monitor.collector import AdvancedCollector, FetchStrategy
from gold_monitor.data_sources.base import BaseDataSource, PriceData
from gold_monitor.models import Database


class FailingSource(BaseDataSource):
    """总是失败的数据源（立即抛异常，会最先完成）"""

    def __init__(self, name: str = "failing"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def fetch_price(self) -> PriceData:
        raise RuntimeError("boom")


class SlowSource(BaseDataSource):
    """较慢但成功的数据源"""

    def __init__(self, price: float, delay: float = 0.05, name: str = "slow"):
        self._price = price
        self._delay = delay
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def fetch_price(self) -> PriceData:
        await asyncio.sleep(self._delay)
        return PriceData(price=self._price, source=self._name)


@pytest.fixture
def collector():
    db = Database("sqlite:///:memory:")
    db.create_tables()
    return AdvancedCollector(database=db, strategy=FetchStrategy.PARALLEL_FIRST)


@pytest.mark.asyncio
async def test_parallel_first_falls_back_when_fastest_fails(collector):
    """最快完成的源失败时，应回退到较慢但成功的源。

    修复前：pending 任务在回退分支前被取消，导致最快源失败即整体失败。
    """
    collector._sources = [FailingSource(), SlowSource(price=1234.5)]

    result = await collector._fetch_parallel_first()

    assert result is not None
    assert result.price == 1234.5
    assert result.source == "slow"


@pytest.mark.asyncio
async def test_parallel_first_returns_none_when_all_fail(collector):
    """所有源都失败时返回 None"""
    collector._sources = [FailingSource("f1"), FailingSource("f2")]

    result = await collector._fetch_parallel_first()

    assert result is None


@pytest.mark.asyncio
async def test_parallel_first_picks_a_successful_source(collector):
    """有多个成功源时，返回其中之一的有效价格"""
    collector._sources = [SlowSource(price=2000.0, delay=0.01, name="a"),
                          SlowSource(price=2001.0, delay=0.02, name="b")]

    result = await collector._fetch_parallel_first()

    assert result is not None
    assert result.price in (2000.0, 2001.0)
