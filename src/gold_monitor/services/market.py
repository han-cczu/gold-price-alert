"""Exchange-rate freshness, conversion and bank quote queries."""

import asyncio
import math
from datetime import timedelta

import httpx

from ..data_sources.bank import BankGoldDataSource
from ..time_utils import utcnow, iso_utc

UNIT_FACTORS = {"oz": 1.0, "g": 31.1035, "kg": 0.0311035}


class DemoBankSource(BankGoldDataSource):
    async def fetch_base_price_cny(self) -> float:
        self._base_price_cny = 550.0
        self._international_price = self._base_price_cny * 31.1035 / 7.2
        self._updated_at = utcnow()
        self._last_fetch_failed = False
        return self._base_price_cny


class MarketService:
    def __init__(self, config, *, client=None, bank_source=None):
        self._client = client or httpx.AsyncClient(timeout=5)
        self._bank = bank_source or (
            DemoBankSource() if config.data_source == "mock" else BankGoldDataSource()
        )
        self._mock = config.data_source == "mock"
        self._rate = 7.2
        self._updated_at = None
        self._last_attempt = None
        self._lock = asyncio.Lock()

    async def exchange_rate(self):
        async with self._lock:
            now = utcnow()
            stale = self._updated_at is None or now - self._updated_at >= timedelta(
                minutes=30
            )
            retry_due = (
                self._last_attempt is None
                or now - self._last_attempt >= timedelta(seconds=30)
            )
            if stale and retry_due and not self._mock:
                self._last_attempt = now
                try:
                    response = await self._client.get(
                        "https://api.exchangerate-api.com/v4/latest/USD"
                    )
                    response.raise_for_status()
                    rate = float(response.json()["rates"]["CNY"])
                    if not math.isfinite(rate) or rate <= 0:
                        raise ValueError("无效汇率")
                    self._rate, self._updated_at = rate, now
                    stale = False
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
            return {
                "usd_cny": self._rate,
                "updated_at": self._updated_at,
                "is_fallback": self._updated_at is None,
                "is_stale": stale,
                "source": "mock"
                if self._mock
                else ("default" if self._updated_at is None else "exchangerate-api"),
            }

    async def convert(self, price, from_unit, to_unit, from_currency, to_currency):
        if not math.isfinite(price) or price < 0:
            raise ValueError("价格必须是有限非负数")
        if from_unit not in UNIT_FACTORS or to_unit not in UNIT_FACTORS:
            raise ValueError("不支持的单位")
        if from_currency not in ("USD", "CNY") or to_currency not in ("USD", "CNY"):
            raise ValueError("不支持的币种")
        rate_data = await self.exchange_rate()
        rate = rate_data["usd_cny"]
        factor = (
            1
            if from_currency == to_currency
            else rate
            if to_currency == "CNY"
            else 1 / rate
        )
        value = price * UNIT_FACTORS[from_unit] / UNIT_FACTORS[to_unit] * factor
        return {
            "original": {"price": price, "unit": from_unit, "currency": from_currency},
            "converted": {
                "price": round(value, 2),
                "unit": to_unit,
                "currency": to_currency,
            },
            "exchange_rate": rate,
        }

    async def bank_prices(self):
        prices = await self._bank.fetch_all_bank_prices()
        # A single acquisition supplies both cards and their comparison baseline.
        base_price = (
            (prices[0].buy_price + prices[0].sell_price) / 2 if prices else None
        )
        return {
            "data": [
                {
                    "bank_name": p.bank_name,
                    "bank_code": p.bank_code,
                    "buy_price": p.buy_price,
                    "sell_price": p.sell_price,
                    "timestamp": iso_utc(p.timestamp),
                    "product_name": p.product_name,
                }
                for p in prices
            ],
            "base_price_cny": base_price,
            "london_gold_cny": base_price,
            "updated_at": iso_utc(prices[0].timestamp) if prices else None,
            "is_fallback": self._bank.is_fallback,
            "is_stale": self._bank.is_fallback,
        }

    async def close(self):
        try:
            await self._bank.close()
        finally:
            await self._client.aclose()
