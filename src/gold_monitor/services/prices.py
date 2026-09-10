"""Price acquisition and bounded history queries shared by the HTTP API."""

from datetime import timedelta

from ..time_utils import utcnow


class PriceUnavailableError(RuntimeError):
    pass


class PriceService:
    def __init__(self, database, collector):
        self.db, self.collector = database, collector

    async def current(self):
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
                await self.current()
                end = utcnow()
                result = await self.db.run(
                    self.db.get_chart_data, start, end, max_points=2000, currency="USD"
                )
            except PriceUnavailableError:
                pass
        return result
