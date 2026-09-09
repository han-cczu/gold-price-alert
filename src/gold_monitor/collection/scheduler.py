"""Periodic collection owns and drains its optional current-sample recovery task."""

import asyncio
from collections.abc import Awaitable, Callable


class CollectionScheduler:
    def __init__(
        self,
        collect: Callable[[], Awaitable[object]],
        recover: Callable[[], Awaitable[object]] | None,
        interval: Callable[[], int],
        failures: Callable[[], int],
        interval_changed: asyncio.Event,
    ):
        self._collect = collect
        self._recover = recover
        self._interval = interval
        self._failures = failures
        self._interval_changed = interval_changed
        self._recovery_task: asyncio.Future | None = None

    async def run(self) -> None:
        try:
            await self._collect()
            if self._recover is not None:
                self._recovery_task = asyncio.ensure_future(self._recover())
            while True:
                failures = self._failures()
                delay = (
                    min(2 ** min(failures, 9), 300) if failures else self._interval()
                )
                try:
                    await asyncio.wait_for(self._interval_changed.wait(), timeout=delay)
                    self._interval_changed.clear()
                except asyncio.TimeoutError:
                    pass
                await self._collect()
        finally:
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                await asyncio.gather(self._recovery_task, return_exceptions=True)
                self._recovery_task = None
