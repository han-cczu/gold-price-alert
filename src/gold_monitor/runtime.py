"""Application-owned services and their deterministic lifecycle."""

import asyncio
import logging
from datetime import datetime, timedelta

from .alert import AlertMonitor
from .collector import AdvancedCollector, FetchStrategy
from .config import Settings
from .data_lifecycle import DataLifecycleManager
from .llm_config import LLMConfigManager
from .models import Database
from .realtime import ConnectionManager
from .security import APIKeyAuth, RateLimiter
from .services.analysis import AnalysisService
from .services.market import MarketService
from .services.notifications import NotificationService
from .services.prices import PriceService
from .metrics import (
    record_price,
    record_alert,
    update_ws_connections,
    record_ws_message,
)
from .time_utils import utcnow, iso_utc

logger = logging.getLogger(__name__)


class ApplicationRuntime:
    def __init__(
        self,
        config: Settings,
        *,
        database=None,
        collector_factory=AdvancedCollector,
        market_factory=MarketService,
        analysis_factory=AnalysisService,
        notification_factory=NotificationService,
        background_tasks=True,
    ):
        self.config = config
        self.database = database
        self.collector_factory, self.market_factory = collector_factory, market_factory
        self.analysis_factory, self.notification_factory = (
            analysis_factory,
            notification_factory,
        )
        self.background_tasks = background_tasks
        self.auth = APIKeyAuth(config=config)
        self.limiter = RateLimiter(config.rate_limit_per_minute)
        self.started = False
        self.started_at = None
        self.tasks: set[asyncio.Task] = set()
        self.collector = self.alert_monitor = self.lifecycle = None
        self.analysis = self.notifications = self.market = self.prices = None
        self.ws = ConnectionManager()
        self.llm_config = None

    @property
    def db(self):
        if self.database is None:
            raise RuntimeError("数据库只在应用生命周期内可用")
        return self.database

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(completed):
            self.tasks.discard(completed)
            if not completed.cancelled() and completed.exception():
                logger.error("后台任务失败: %s", type(completed.exception()).__name__)

        task.add_done_callback(finished)
        return task

    async def start(self):
        if self.started:
            return
        try:
            if self.database is None:
                self.database = Database(self.config.database_url)
            await self.db.run(self.db.create_tables)
            self.llm_config = LLMConfigManager(settings=self.config)
            self.analysis = self.analysis_factory(self.llm_config, settings=self.config)
            self.market = self.market_factory(self.config)
            self.notifications = self.notification_factory(self.db, self.config)
            channels = await self.notifications.active_channels()
            self.alert_monitor = await self.db.run(
                AlertMonitor,
                self.db,
                channels=channels,
                config=self.config,
                volatility_window_minutes=self.config.alert_volatility_window,
            )
            self.notifications.monitor = self.alert_monitor
            strategy = (
                FetchStrategy.FALLBACK
                if self.config.data_source == "fallback"
                else FetchStrategy.SINGLE
            )
            self.collector = self.collector_factory(
                self.db,
                strategy=strategy,
                config=self.config,
                on_price_update=self.on_price_update,
                deduplicate=True,
                gap_detection=True,
            )
            self.prices = PriceService(self.db, self.collector)
            self.lifecycle = DataLifecycleManager(self.db, config=self.config)
            self.started_at, self.started = utcnow(), True
            if self.background_tasks:
                self.collector.start(self.config.fetch_interval)
                self.spawn(self._cleanup_loop())
                self.spawn(self._daily_analysis())
                self.spawn(self.analysis.run_smart())
        except BaseException:
            await self.close()
            raise

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(24 * 3600)
            await self.lifecycle.cleanup()

    async def _daily_analysis(self):
        while True:
            now = datetime.now()
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            await asyncio.sleep((tomorrow - now).total_seconds())
            try:
                await self.analysis.run_smart(force=True)
            except Exception:
                logger.exception("每日分析失败")

    async def on_price_update(self, price):
        if price.currency == "USD":
            record_price(price.price)
        update_ws_connections(self.ws.connection_count)
        alerts = (
            await self.alert_monitor.check_price(price) if self.alert_monitor else []
        )
        await self.ws.broadcast(
            {
                "type": "price_update",
                "data": {
                    "price": price.price,
                    "currency": price.currency,
                    "source": price.source,
                    "timestamp": iso_utc(price.timestamp),
                    "recorded": price.recorded,
                },
            }
        )
        record_ws_message("price_update")
        for alert in alerts:
            record_alert(alert.alert_type.value)
            await self.ws.broadcast(
                {
                    "type": "alert",
                    "data": {
                        "alert_type": alert.alert_type.value,
                        "price": alert.price,
                        "message": alert.message,
                        "triggered_at": iso_utc(alert.triggered_at),
                    },
                }
            )
            record_ws_message("alert")

    async def close(self):
        self.started = False

        # Every cleanup gets a chance even if a preceding resource fails.
        async def release(action):
            try:
                await action()
            except Exception:
                logger.exception("关闭应用资源失败")

        if self.collector:
            await release(self.collector.stop)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self.analysis:
            await release(self.analysis.close)
        if self.alert_monitor:
            await release(self.alert_monitor.wait_pending_notifications)
            await release(lambda: self.db.run(self.alert_monitor.force_persist))
        await release(self.ws.close)
        if self.market:
            await release(self.market.close)
        if self.database:
            await release(self.database.aclose)
            self.database = None
