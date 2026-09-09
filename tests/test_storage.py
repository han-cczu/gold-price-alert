"""Repository compatibility, transaction and worker-thread regressions."""

import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident

import pytest
from sqlalchemy import event

from gold_monitor.models import AlertRecord, AlertState, Database, GoldPrice
from gold_monitor.storage import Database as StorageDatabase


@pytest.fixture
def database():
    db = Database("sqlite:///:memory:")
    db.create_tables()
    yield db
    db.close()


def test_models_database_is_compatible_export():
    assert Database is StorageDatabase


async def test_async_operations_share_memory_database_without_sharing_sessions(
    database,
):
    main_thread = get_ident()
    worker_threads = [await database.run(get_ident) for _ in range(3)]
    assert len(set(worker_threads)) == 1
    assert worker_threads[0] != main_thread
    quote = datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    await database.run(
        database.save_price, 2000, "source", timestamp=quote, currency="EUR"
    )
    row = await database.run(database.get_latest_price)
    assert row.timestamp == datetime(2026, 1, 1)
    assert row.currency == "EUR"
    assert database.get_price_count() == 1


def test_history_query_applies_limit_order_and_range(database):
    start = datetime(2026, 1, 1)
    for index in range(5):
        database.save_price(2000 + index, timestamp=start + timedelta(minutes=index))
    end = start + timedelta(hours=1)
    assert [p.price for p in database.get_prices_in_range(start, end, 2)] == [
        2000,
        2001,
    ]
    assert [
        p.price for p in database.get_prices_in_range(start, end, 2, newest_first=True)
    ] == [2004, 2003]
    with pytest.raises(ValueError):
        database.get_prices_in_range(start, end, 0)


def test_alert_and_cooldown_commit_together(database):
    now = datetime(2026, 1, 1)
    record = database.save_alert_with_state(
        "threshold_upper",
        2600,
        "test",
        triggered_at=now,
        cooldown_until=now + timedelta(minutes=5),
    )
    state = database.get_alert_state("threshold_upper")
    assert record.id is not None
    assert record.triggered_at == state.last_triggered_at == now
    assert state.cooldown_until == now + timedelta(minutes=5)


def test_alert_state_failure_rolls_back_already_flushed_alert(database):
    def fail_state(mapper, connection, target):
        raise RuntimeError("state write failed")

    now = datetime(2026, 1, 1)
    event.listen(AlertState, "before_insert", fail_state)
    try:
        with pytest.raises(RuntimeError, match="state write failed"):
            database.save_alert_with_state(
                "threshold_upper",
                2600,
                "test",
                triggered_at=now,
                cooldown_until=now + timedelta(minutes=5),
            )
    finally:
        event.remove(AlertState, "before_insert", fail_state)
    with database.get_session() as session:
        assert session.query(AlertRecord).count() == 0
        assert session.query(AlertState).count() == 0


async def test_cancelled_worker_is_drained_before_cancellation_returns(database):
    started = Event()
    release = Event()

    def work():
        started.set()
        assert release.wait(timeout=2)
        return database.save_price(2000)

    task = asyncio.create_task(database.run(work))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert database.get_price_count() == 1


async def test_close_is_idempotent_and_rejects_new_operations(database):
    await database.aclose()
    database.dispose()
    with pytest.raises(RuntimeError, match="数据库已关闭"):
        await database.run(database.get_price_count)


async def test_close_drains_previously_queued_operations(database):
    started, release = Event(), Event()
    written = []

    def work():
        started.set()
        assert release.wait(timeout=2)
        written.append(database.save_price(2000).id)

    task = asyncio.create_task(database.run(work))
    while not started.is_set():
        await asyncio.sleep(0)
    close = asyncio.create_task(database.aclose())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task, close)
    assert len(written) == 1


def test_chart_samples_cover_complete_window_and_keep_extrema(database):
    start = datetime(2020, 1, 1)
    prices = [2000 + index % 71 for index in range(12000)]
    prices[11], prices[102] = 999, 4000
    end = start + timedelta(hours=len(prices) - 1)
    with database.get_session() as session:
        session.add_all(
            [
                GoldPrice(
                    price=price,
                    timestamp=start + timedelta(hours=index),
                    source="test",
                    currency="USD",
                )
                for index, price in enumerate(prices)
            ]
        )
        session.add_all(
            [
                GoldPrice(
                    price=1,
                    timestamp=start - timedelta(seconds=1),
                    source="outside",
                    currency="USD",
                ),
                GoldPrice(
                    price=99999,
                    timestamp=end + timedelta(seconds=1),
                    source="outside",
                    currency="USD",
                ),
                GoldPrice(
                    price=999999, timestamp=start, source="other", currency="CNY"
                ),
            ]
        )
        session.commit()
    result = database.get_chart_data(start, end, max_points=100)
    assert 2 <= len(result["prices"]) <= 100
    assert len(result["prices"]) == len(result["timestamps"])
    assert result["timestamps"][0] == "2020-01-01T00:00:00Z"
    assert result["timestamps"][-1] == end.replace(
        tzinfo=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    assert result["prices"][0] == prices[0]
    assert result["prices"][-1] == prices[-1]
    assert min(result["prices"]) == result["low"] == 999
    assert max(result["prices"]) == result["high"] == 4000
    assert result["count"] == len(prices)
    assert result["average"] == pytest.approx(sum(prices) / len(prices))
    assert result["current_price"] == prices[-1]
    assert result["price_change"] == prices[-1] - prices[0]
    assert result["price_change_percent"] == pytest.approx(
        (prices[-1] - prices[0]) / prices[0] * 100
    )
    assert result["window_start"] == result["timestamps"][0]
    assert result["window_end"] == result["timestamps"][-1]


@pytest.mark.parametrize("max_points", [2, 3, 4, 5, 15, 30])
def test_chart_point_limit_includes_endpoints_even_with_identical_timestamps(
    database, max_points
):
    instant = datetime(2026, 1, 1)
    for index in range(20):
        database.save_price(2000 + index, timestamp=instant)
    result = database.get_chart_data(instant, instant, max_points)
    assert len(result["prices"]) <= max_points
    assert result["prices"][0] == 2000
    assert result["prices"][-1] == 2019
    assert result["count"] == 20
    assert result["average"] == 2009.5


def test_chart_empty_window_has_explicit_bounds_and_zero_count(database):
    start = datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    end = start + timedelta(hours=1)
    result = database.get_chart_data(start, end)
    assert result["prices"] == result["timestamps"] == []
    assert result["count"] == result["average"] == result["high"] == result["low"] == 0
    assert result["window_start"] == "2026-01-01T00:00:00Z"
    assert result["window_end"] == "2026-01-01T01:00:00Z"


def test_chart_uses_requested_currency(database):
    instant = datetime(2026, 1, 1)
    database.save_price(2000, timestamp=instant, currency="USD")
    database.save_price(450, timestamp=instant, currency="CNY")
    result = database.get_chart_data(instant, instant, currency="CNY")
    assert result["prices"] == [450]
    assert result["count"] == 1


@pytest.mark.parametrize("max_points", [0, 1, -1, 100001])
def test_chart_rejects_invalid_point_limit(database, max_points):
    instant = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        database.get_chart_data(instant, instant, max_points)


def test_price_queries_filter_currency_before_order_and_limit(database):
    start = datetime(2026, 1, 1)
    for index, currency in enumerate(["CNY", "USD", "CNY", "USD", "CNY"]):
        database.save_price(
            2000 + index, currency=currency, timestamp=start + timedelta(minutes=index)
        )
    end = start + timedelta(hours=1)
    assert database.get_latest_price().price == 2004
    assert database.get_latest_price(currency="USD").price == 2003
    assert database.get_latest_price(currency="EUR") is None
    assert [record.price for record in database.get_recent_prices(2)] == [2004, 2003]
    assert [
        record.price for record in database.get_recent_prices(2, currency="USD")
    ] == [2003, 2001]
    assert [
        record.price
        for record in database.get_prices_in_range(start, end, 1, currency="USD")
    ] == [2001]
    assert [
        record.price
        for record in database.get_prices_in_range(
            start, end, 1, currency="USD", newest_first=True
        )
    ] == [2003]
    assert [
        record.price
        for record in database.get_prices_in_range(
            start, end, 1, currency="USD", offset=1
        )
    ] == [2003]
