"""HTTP price reads reuse the collector's quote instead of driving upstream fetches."""

import asyncio
from datetime import datetime, timedelta

import pytest

from gold_monitor.data_sources.base import PriceData
from gold_monitor.services.prices import PriceService, PriceUnavailableError

NOW = datetime(2026, 9, 12, 12, 0, 0)


class FakeCollector:
    def __init__(self, interval=30):
        self.interval = interval
        self.last_price = None
        self.fetches = 0
        self.next_price = PriceData(2000, source="fake", timestamp=NOW)

    async def fetch_once(self):
        self.fetches += 1
        if self.next_price is not None:
            self.last_price = self.next_price
        return self.next_price


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def collector():
    return FakeCollector()


@pytest.fixture
def service(collector, clock):
    return PriceService(None, collector, clock=lambda: clock[0])


async def test_fresh_collector_quote_is_served_without_any_fetch(
    service, collector, clock
):
    collector.last_price = PriceData(
        2100, source="fake", timestamp=clock[0] - timedelta(seconds=20)
    )
    for _ in range(20):
        assert (await service.current()).price == 2100
    assert collector.fetches == 0


async def test_stale_quote_refreshes_at_most_once_per_interval(
    service, collector, clock
):
    stale = clock[0] - timedelta(minutes=10)
    collector.last_price = PriceData(2100, source="fake", timestamp=stale)
    # The upstream keeps returning an old observation, so every read stays stale.
    collector.next_price = PriceData(2200, source="fake", timestamp=stale)
    assert (await service.current()).price == 2200
    assert collector.fetches == 1
    for _ in range(5):
        assert (await service.current()).price == 2200
    assert collector.fetches == 1, "reads inside one interval must not refetch"
    clock[0] += timedelta(seconds=collector.interval)
    await service.current()
    assert collector.fetches == 2


async def test_no_quote_and_failed_refresh_is_unavailable(service, collector):
    collector.next_price = None
    with pytest.raises(PriceUnavailableError):
        await service.current()
    with pytest.raises(PriceUnavailableError):
        await service.current()
    assert collector.fetches == 1, "a failed refresh is not retried inside one interval"


async def test_concurrent_readers_share_one_refresh(clock):
    gate = asyncio.Event()

    class SlowCollector(FakeCollector):
        async def fetch_once(self):
            self.fetches += 1
            await gate.wait()
            self.last_price = self.next_price
            return self.next_price

    collector = SlowCollector()
    collector.next_price = PriceData(2300, source="fake", timestamp=clock[0])
    service = PriceService(None, collector, clock=lambda: clock[0])
    tasks = [asyncio.create_task(service.current()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert [price.price for price in results] == [2300] * 5
    assert collector.fetches == 1


async def test_chart_fallback_uses_the_bounded_refresh(collector, clock):
    class Database:
        def __init__(self):
            self.calls = 0

        def get_chart_data(self, *args, **kwargs):
            raise AssertionError("run() stands in for the executor")

        async def run(self, function, *args, **kwargs):
            self.calls += 1
            return {"count": 0}

    database = Database()
    service = PriceService(database, collector, clock=lambda: clock[0])
    await service.chart(24)
    await service.chart(24)
    # Each empty window queries, refreshes and queries again, but the second
    # refresh reuses the fresh quote instead of fetching upstream again.
    assert collector.fetches == 1
    assert database.calls == 4
