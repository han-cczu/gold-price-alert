"""数据源测试"""

import asyncio

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
    fallback = FallbackDataSource(
        [_AlwaysFail("primary"), MockDataSource(base_price=2000.0)]
    )

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


async def test_fallback_closes_every_owned_source_after_one_close_fails():
    closed = []

    class OwnedSource(_AlwaysFail):
        async def close(self):
            closed.append(self.name)
            if self.name == "first":
                raise RuntimeError("close failed")

    source = FallbackDataSource([OwnedSource("first"), OwnedSource("second")])
    await source.close()
    assert closed == ["first", "second"]


def test_source_factory_uses_instance_configuration():
    from gold_monitor.config import Settings
    from gold_monitor.data_sources.factory import create_data_source

    config = Settings(
        _env_file=None, data_source="goldapi", goldapi_key="local-test-key"
    )
    source = create_data_source(config=config)
    assert source.name == "goldapi"


@pytest.mark.parametrize("retries", [0, -1])
def test_fallback_requires_positive_retry_count(retries):
    with pytest.raises(ValueError):
        FallbackDataSource([_AlwaysFail()], max_retries=retries)


def test_production_fallback_contains_only_real_sources():
    from gold_monitor.config import Settings
    from gold_monitor.data_sources.factory import (
        create_data_source,
        create_fallback_source,
    )

    config = Settings(_env_file=None, data_source="fallback", goldapi_key="test-only")
    fallback = create_fallback_source(config)
    assert [source.name for source in fallback._sources] == ["goldapi", "sina"]
    without_key = create_fallback_source(config.model_copy(update={"goldapi_key": ""}))
    assert [source.name for source in without_key._sources] == ["sina"]
    assert isinstance(create_data_source("mock", config), MockDataSource)


async def test_fallback_rechecks_primary_after_recovery():
    class RecoveringSource(_AlwaysFail):
        healthy = False
        calls = 0

        async def fetch_price(self):
            self.calls += 1
            if not self.healthy:
                raise ConnectionError("temporary outage")
            return PriceData(2300, source=self.name)

    primary, backup = RecoveringSource("primary"), RecoveringSource("backup")
    backup.healthy = True
    fallback = FallbackDataSource([primary, backup])
    assert (await fallback.fetch_price()).source == "backup"
    primary.healthy = True
    assert (await fallback.fetch_price()).source == "primary"
    assert fallback.active_source is primary
    assert primary.calls == 3 and backup.calls == 1


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1])
async def test_invalid_primary_quote_uses_valid_backup(price):
    class InvalidPrice(_AlwaysFail):
        async def fetch_price(self):
            return PriceData(price, source=self.name)

    fallback = FallbackDataSource([InvalidPrice(), MockDataSource(base_price=2300)])
    assert (await fallback.fetch_price()).source == "mock"


async def test_cancelled_fallback_drains_child_without_starting_backup():
    started, cancelled = asyncio.Event(), asyncio.Event()

    class HangingSource(_AlwaysFail):
        async def fetch_price(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class UnusedBackup(_AlwaysFail):
        calls = 0

        async def fetch_price(self):
            self.calls += 1
            return PriceData(2300, source=self.name)

    backup = UnusedBackup("backup")
    fallback = FallbackDataSource([HangingSource("primary"), backup])
    task = asyncio.create_task(fallback.fetch_price())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and backup.calls == 0


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_fallback_rejects_invalid_source_budget(timeout):
    with pytest.raises(ValueError):
        FallbackDataSource([_AlwaysFail()], source_timeout=timeout)
