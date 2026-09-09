"""采集器测试 - 重点验证 PARALLEL_FIRST 容错回退"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from gold_monitor.collector import AdvancedCollector, FetchStrategy
from gold_monitor.data_sources.base import BaseDataSource, PriceData
from gold_monitor.models import Database
from gold_monitor.config import Settings


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
    yield AdvancedCollector(database=db, strategy=FetchStrategy.PARALLEL_FIRST)
    db.close()


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
    collector._sources = [
        SlowSource(price=2000.0, delay=0.01, name="a"),
        SlowSource(price=2001.0, delay=0.02, name="b"),
    ]

    result = await collector._fetch_parallel_first()

    assert result is not None
    assert result.price in (2000.0, 2001.0)


class SequenceSource(BaseDataSource):
    def __init__(self, quotes, name="sequence"):
        self.quotes = iter(quotes)
        self._name = name
        self.closed = False

    @property
    def name(self):
        return self._name

    async def fetch_price(self):
        return next(self.quotes)

    async def close(self):
        self.closed = True


async def test_collect_preserves_quote_and_deduplicates_from_last_insert(collector):
    callback_flags = []
    collector._on_price_update = lambda quote: callback_flags.append(quote.recorded)
    instant = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    collector._sources = [
        SequenceSource(
            [
                PriceData(
                    price=2000.0, currency="CNY", timestamp=instant, source="test"
                ),
                PriceData(
                    price=2000.006, currency="CNY", timestamp=instant, source="test"
                ),
                PriceData(
                    price=2000.012, currency="CNY", timestamp=instant, source="test"
                ),
            ]
        )
    ]
    results = [await collector.collect() for _ in range(3)]
    assert [result.saved for result in results] == [True, False, True]
    assert callback_flags == [True, False, True]
    records = collector._db.get_recent_prices()
    assert len(records) == 2
    assert {record.currency for record in records} == {"CNY"}
    assert {record.timestamp for record in records} == {datetime(2025, 12, 31, 16)}
    assert collector.stats.saved_count == 2
    assert collector.stats.skipped_count == 1
    await collector.stop()


async def test_gap_sample_count_excludes_deduplicated_quote(collector):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    collector._db.save_price(1999, timestamp=now - timedelta(minutes=20))
    collector._db.save_price(2000, timestamp=now - timedelta(minutes=10))
    collector._sources = [SequenceSource([PriceData(2000), PriceData(2000)])]
    await collector.fetch_once()
    before = collector._db.get_price_count()
    assert await collector.fill_gaps() == 0
    assert collector._db.get_price_count() == before
    assert collector.stats.gaps_filled == 0
    await collector.stop()


async def test_strategy_change_replaces_and_closes_sources(collector, monkeypatch):
    previous = SequenceSource([], "previous")
    replacement = SequenceSource([], "replacement")
    collector._sources = [previous]
    monkeypatch.setattr(collector, "_make_sources", lambda strategy: [replacement])
    result = await collector.reconfigure(interval=15, strategy="fallback")
    assert result["strategy"] == "fallback"
    assert result["sources"] == ["replacement"]
    assert previous.closed and not replacement.closed
    assert collector.interval == 15
    await collector.stop()
    assert replacement.closed


async def test_reconfigure_waits_for_inflight_quote_before_closing_source(
    collector, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()

    class InFlightSource(SequenceSource):
        async def fetch_price(self):
            started.set()
            await release.wait()
            assert not self.closed
            return PriceData(2000)

    previous = InFlightSource([])
    replacement = SequenceSource([])
    collector._sources = [previous]
    monkeypatch.setattr(collector, "_make_sources", lambda _: [replacement])
    fetch = asyncio.create_task(collector.fetch_once())
    await started.wait()
    change = asyncio.create_task(collector.reconfigure(strategy="single"))
    await asyncio.sleep(0)
    assert not previous.closed
    assert not change.done()
    release.set()
    assert (await fetch).price == 2000
    await change
    assert previous.closed
    await collector.stop()


async def test_parallel_vote_keeps_existing_upper_middle_sample_selection(collector):
    collector._sources = [
        SlowSource(price, delay=0) for price in [2000, 1999, 2100, 2010]
    ]
    result = await collector._fetch_parallel_vote()
    assert result.price == 2010
    await collector.stop()


@pytest.mark.parametrize("strategy", ["invalid", "single"])
async def test_invalid_reconfiguration_is_atomic(collector, monkeypatch, strategy):
    original = collector.get_config()

    def invalid_source(_):
        raise ValueError("missing key")

    monkeypatch.setattr(collector, "_make_sources", invalid_source)
    with pytest.raises(ValueError):
        await collector.reconfigure(interval=15, strategy=strategy)
    assert collector.get_config() == original


async def test_parallel_completion_cancels_and_drains_other_sources(collector):
    cancelled = asyncio.Event()

    class NeverFinishes(SlowSource):
        async def fetch_price(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    collector._sources = [
        NeverFinishes(2000, name="waiting"),
        SlowSource(2001, delay=0.01),
    ]
    assert (await collector.fetch_once()).price == 2001
    assert cancelled.is_set()
    await collector.stop()


async def test_stop_drains_current_sample_recovery(collector, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    source = SequenceSource([], "owned")
    collector._sources = [source]

    async def fetch():
        return None

    async def fill(hours):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    collector._scheduler._collect = fetch
    monkeypatch.setattr(collector, "fill_gaps", fill)
    collector.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await collector.stop()
    assert cancelled.is_set()
    assert source.closed
    assert collector._scheduler._recovery_task is None
    assert not collector.is_running


async def test_collector_uses_explicit_configuration(collector):
    custom = Settings(_env_file=None, data_source="mock", fetch_interval=17)
    instance = AdvancedCollector(collector._db, FetchStrategy.SINGLE, config=custom)
    assert instance.interval == 17
    assert (await instance.fetch_once()).source == "mock"
    await instance.stop()


async def test_database_failure_is_not_counted_as_success(collector, monkeypatch):
    collector._sources = [SequenceSource([PriceData(2000)])]

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(collector._db, "save_price", fail)
    result = await collector.collect()
    assert result.error == "disk full"
    assert result.price is None and not result.saved
    assert collector.stats.success_count == collector.stats.saved_count == 0
    assert collector.stats.failure_count == 1
    await collector.stop()
