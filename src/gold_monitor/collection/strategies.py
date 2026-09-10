"""Source selection; task cancellation stays inside the strategy that created it."""

import asyncio
from enum import Enum
from typing import Awaitable, Callable

from ..data_sources.base import BaseDataSource, PriceData


class FetchStrategy(Enum):
    """采集策略"""

    SINGLE = "single"  # 单数据源
    FALLBACK = "fallback"  # 顺序降级
    PARALLEL_FIRST = "parallel_first"  # 并行取最快
    PARALLEL_VOTE = "parallel_vote"  # 并行投票（取中位数）


Fetch = Callable[[BaseDataSource], Awaitable[tuple[str, PriceData | None, float]]]


async def parallel_first(
    sources: list[BaseDataSource], fetch: Fetch
) -> PriceData | None:
    tasks = {asyncio.ensure_future(fetch(source)) for source in sources}
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                _, price, _ = task.result()
                if price is not None:
                    return price
        return None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def parallel_vote(
    sources: list[BaseDataSource], fetch: Fetch
) -> PriceData | None:
    tasks = [asyncio.ensure_future(fetch(source)) for source in sources]
    try:
        results = await asyncio.gather(*tasks)
        prices = [price for _, price, _ in results if price is not None]
        if not prices:
            return None
        # Preserve the existing upper-middle sample selection for an even count.
        return sorted(prices, key=lambda price: price.price)[len(prices) // 2]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
