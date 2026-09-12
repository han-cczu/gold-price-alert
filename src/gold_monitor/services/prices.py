"""Price acquisition and bounded history queries shared by the HTTP API."""

import asyncio
from datetime import timedelta

from ..collection.stats import freshness_limit_seconds
from ..time_utils import utcnow


class PriceUnavailableError(RuntimeError):
    pass


class PriceService:
    """Serve the collector's quote without letting callers drive upstream fetches.

    ``/api/price/current`` used to fetch from the upstream source, persist and
    evaluate alerts on every request, so anonymous traffic could exhaust vendor
    quotas. The collector already samples on schedule: reads reuse its latest
    quote and refresh only when that quote is stale, at most once per interval.
    """

    def __init__(self, database, collector, *, clock=utcnow):
        self.db, self.collector = database, collector
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_at = None

    def _fresh_quote(self):
        price = self.collector.last_price
        if price is None or price.timestamp is None:
            return None
        age = (self._clock() - price.timestamp).total_seconds()
        limit = freshness_limit_seconds(self.collector.interval)
        return price if 0 <= age <= limit else None

    async def current(self):
        """The latest quote; a stale quote triggers one bounded refresh attempt."""
        price = self._fresh_quote()
        if price is not None:
            return price
        try:
            return await self.refresh()
        except PriceUnavailableError:
            # A dated quote carrying its own timestamp beats an error while the
            # collector is between retries; only "never received" is an error.
            price = self.collector.last_price
            if price is None:
                raise
            return price

    async def refresh(self):
        """Fetch through the collector, shared by concurrent callers and spaced
        at least one collection interval apart."""
        async with self._refresh_lock:
            price = self._fresh_quote()
            if price is not None:
                return price
            now = self._clock()
            spacing = timedelta(seconds=self.collector.interval)
            if (
                self._last_refresh_at is not None
                and now - self._last_refresh_at < spacing
            ):
                raise PriceUnavailableError("行情数据源暂不可用")
            self._last_refresh_at = now
            price = await self.collector.fetch_once()
        if price is None:
            raise PriceUnavailableError("行情数据源暂不可用")
        return price

    async def latest(self):
        return await self.db.run(self.db.get_latest_price)

    async def history(self, hours, limit):
        end = utcnow()
        records = await self.db.run(
            self.db.get_prices_in_range,
            end - timedelta(hours=hours),
            end,
            limit=limit,
            newest_first=True,
        )
        return list(reversed(records))

    async def chart(self, hours):
        end = utcnow()
        start = end - timedelta(hours=hours)
        result = await self.db.run(
            self.db.get_chart_data, start, end, max_points=2000, currency="USD"
        )
        if not result["count"]:
            try:
                await self.refresh()
                end = utcnow()
                result = await self.db.run(
                    self.db.get_chart_data, start, end, max_points=2000, currency="USD"
                )
            except PriceUnavailableError:
                pass
        return result
