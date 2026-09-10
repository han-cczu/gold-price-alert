"""Bank reference quotes retain coherent successful snapshots on upstream failure."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from gold_monitor.data_sources.bank import BankGoldDataSource
from gold_monitor.dependencies import get_runtime
from gold_monitor.routers.market import router
from gold_monitor.services.market import MarketService


class QuoteFeed:
    def __init__(self):
        self.quote_status = 200
        self.quote_text = 'var hq_str_hf_GC="3000,0";'
        self.quote_error = False
        self.rate_status = 200
        self.rate_json = {"rates": {"CNY": 7.2}}
        self.rate_error = False

    def respond(self, request):
        if request.url.host == "hq.sinajs.cn":
            if self.quote_error:
                raise httpx.ReadTimeout("offline quote timeout", request=request)
            return httpx.Response(self.quote_status, text=self.quote_text)
        assert request.url.host == "api.exchangerate-api.com"
        if self.rate_error:
            raise httpx.ReadTimeout("offline rate timeout", request=request)
        return httpx.Response(self.rate_status, json=self.rate_json)


@pytest.fixture
async def bank_market(app_settings):
    feed = QuoteFeed()
    client = httpx.AsyncClient(transport=httpx.MockTransport(feed.respond))
    source = BankGoldDataSource()
    source._client = client
    service = MarketService(app_settings, client=client, bank_source=source)
    yield service, source, feed
    await service.close()
    assert client.is_closed


FAILURES = [
    ("quote_status", 503),
    ("quote_error", True),
    ("quote_status", 201),
    ("quote_text", "<html>maintenance</html>"),
    ("quote_text", 'var hq_str_hf_GC=",";'),
    ("quote_text", 'var hq_str_hf_GC="NaN,0";'),
    ("quote_text", 'var hq_str_hf_GC="Infinity,0";'),
    ("quote_text", 'var hq_str_hf_GC="-3000,0";'),
    ("quote_text", 'var hq_str_hf_GC="0,0";'),
    ("rate_status", 503),
    ("rate_error", True),
    ("rate_json", {"rates": {}}),
    ("rate_json", {"rates": {"CNY": None}}),
    ("rate_json", {"rates": {"CNY": "NaN"}}),
    ("rate_json", {"rates": {"CNY": "Infinity"}}),
    ("rate_json", {"rates": {"CNY": 0}}),
    ("rate_json", {"rates": {"CNY": -7.2}}),
    ("rate_json", {"rates": {"CNY": True}}),
]


@pytest.mark.parametrize("field,value", FAILURES)
async def test_upstream_failures_preserve_successful_prices_and_time(
    bank_market, field, value
):
    service, source, feed = bank_market
    initial = await service.bank_prices()
    assert initial["base_price_cny"] == round(3000 * 7.2 / 31.1035, 2)
    assert initial["is_fallback"] is False
    updated = source.updated_at
    setattr(feed, field, value)
    failed = await service.bank_prices()
    assert failed["is_fallback"] is True
    assert failed["is_stale"] is True
    for key in ("data", "base_price_cny", "london_gold_cny", "updated_at"):
        assert failed[key] == initial[key]
    assert source.updated_at == updated


@pytest.mark.parametrize("field,value", FAILURES)
async def test_initial_failure_is_explicitly_unavailable(bank_market, field, value):
    service, source, feed = bank_market
    setattr(feed, field, value)
    result = await service.bank_prices()
    assert result == {
        "data": [],
        "base_price_cny": None,
        "london_gold_cny": None,
        "updated_at": None,
        "is_fallback": True,
        "is_stale": True,
    }
    assert source.updated_at is None


async def test_success_after_failure_replaces_cache_and_clears_stale(bank_market):
    service, source, feed = bank_market
    first = await service.bank_prices()
    feed.quote_status = 503
    assert (await service.bank_prices())["is_stale"] is True
    feed.quote_status = 200
    feed.quote_text = 'var hq_str_hf_GC="3100,0";'
    result = await service.bank_prices()
    assert result["base_price_cny"] == round(3100 * 7.2 / 31.1035, 2)
    assert result["is_stale"] is False
    assert result["is_fallback"] is False
    assert result["updated_at"] >= first["updated_at"]
    assert all(row["timestamp"] == result["updated_at"] for row in result["data"])
    price = await source.fetch_price()
    assert price.price == 3100
    assert price.currency == "USD"


async def test_bank_route_serializes_unavailable_and_cached_snapshots(bank_market):
    service, source, feed = bank_market
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_runtime] = lambda: SimpleNamespace(market=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://offline.test"
    ) as api:
        feed.quote_text = 'var hq_str_hf_GC="NaN,0";'
        unavailable = await api.get("/api/bank-prices")
        assert unavailable.status_code == 200
        assert unavailable.json()["data"] == []
        assert unavailable.json()["london_gold_cny"] is None
        assert unavailable.json()["updated_at"] is None
        feed.quote_text = 'var hq_str_hf_GC="3000,0";'
        valid = (await api.get("/api/bank-prices")).json()
        assert valid["updated_at"].endswith("Z")
        feed.rate_status = 503
        cached = (await api.get("/api/bank-prices")).json()
        assert cached["data"] == valid["data"]
        assert cached["updated_at"] == valid["updated_at"]
        assert cached["is_stale"] is True


async def test_concurrent_bank_fetches_publish_coherent_snapshots(app_settings):
    entered, release = asyncio.Event(), asyncio.Event()
    quotes = 0

    async def handler(request):
        nonlocal quotes
        if request.url.host == "hq.sinajs.cn":
            quotes += 1
            if quotes == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, text=f'var hq_str_hf_GC="{3000 + quotes},0";')
        return httpx.Response(200, json={"rates": {"CNY": 7.2}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = BankGoldDataSource()
    source._client = client
    service = MarketService(app_settings, client=client, bank_source=source)
    first = asyncio.create_task(service.bank_prices())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(service.bank_prices())
        await asyncio.sleep(0)
        assert quotes == 1, "a second fetch must not overwrite an in-flight snapshot"
        release.set()
        results = await asyncio.gather(first, second)
        assert [result["base_price_cny"] for result in results] == [
            round(price * 7.2 / 31.1035, 2) for price in (3001, 3002)
        ]
        for result in results:
            assert all(
                row["timestamp"] == result["updated_at"] for row in result["data"]
            )
    finally:
        release.set()
        await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True
        )
        await service.close()


async def test_mock_bank_quotes_are_available_without_network(app_settings):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))
    )
    service = MarketService(app_settings, client=client)
    try:
        result = await service.bank_prices()
        assert result["base_price_cny"] == 550
        assert result["data"]
        assert result["is_fallback"] is False
        assert result["is_stale"] is False
        assert datetime.fromisoformat(result["updated_at"]).tzinfo == timezone.utc
    finally:
        await service.close()
