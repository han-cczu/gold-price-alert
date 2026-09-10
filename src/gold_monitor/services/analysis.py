"""Application-owned analysis work, configuration-aware cache, and cancellation."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from ..analysis.factory import provider_from_config
from ..analysis.providers import LLMProvider, MockLLMProvider
from ..config import Settings
from ..llm_config import LLMConfig, LLMConfigManager
from ..models import Database
from ..time_utils import iso_utc, utcnow

ProviderFactory = Callable[[LLMConfig, str | None, Settings], LLMProvider]


class AnalysisDataError(ValueError):
    """Local samples are insufficient to construct an analysis context."""


class AnalysisService:
    """Coalesce identical in-flight work without coupling caller cancellation."""

    def __init__(
        self,
        config_manager: LLMConfigManager,
        *,
        settings: Settings | None = None,
        provider_factory: ProviderFactory | None = None,
        database: Database | None = None,
    ):
        self.config_manager = config_manager
        self.settings = settings or config_manager.settings
        self._provider_factory = provider_factory or provider_from_config
        self._database = database
        self._cache: dict[str, Any] | None = None
        self._cache_key: tuple[str, str | None, int] | None = None
        self._inflight: dict[tuple[str, str | None, int], asyncio.Task] = {}
        self._local_tasks: set[asyncio.Task] = set()
        self._generation = 0
        self._closed = False

    def attach_database(self, database: Database) -> None:
        """Bind runtime storage before work starts, preserving factory signatures."""
        if self._closed or self._inflight or self._local_tasks:
            raise RuntimeError("只能在分析任务启动前绑定数据库")
        self._database = database

    @staticmethod
    def _model_identity(
        config: LLMConfig, provider: LLMProvider, model: str | None = None
    ) -> tuple[str | None, str | None]:
        if isinstance(provider, MockLLMProvider):
            return "mock", "Mock"
        active = config.get_active_provider()
        return (
            active.id if active is not None else None,
            getattr(provider, "model", model or config.active_model or None),
        )

    @staticmethod
    async def _save_report(
        database: Database,
        analysis_type: str,
        config: LLMConfig,
        provider: LLMProvider,
        result: dict[str, Any],
        *,
        input_summary: str,
        model: str | None = None,
        price_range_start: datetime | None = None,
        price_range_end: datetime | None = None,
    ) -> None:
        provider_id, model_name = AnalysisService._model_identity(
            config, provider, model
        )
        stored = deepcopy(result)
        stored["generated_at"] = iso_utc(stored["generated_at"])
        await database.run(
            database.save_analysis_record,
            analysis_type,
            model_provider=provider_id,
            model_name=model_name,
            price_range_start=price_range_start,
            price_range_end=price_range_end,
            input_summary=input_summary,
            result=stored,
        )

    def _key(self, config: LLMConfig, model: str | None) -> tuple[str, str | None, int]:
        return (
            self.config_manager.config_fingerprint(config),
            model or config.active_model or None,
            self._generation,
        )

    def get_cache(self) -> dict[str, Any] | None:
        if self._cache_key is None:
            return None
        config = self.config_manager.reload_config()
        # A cache for an explicitly selected model remains the last report, but a
        # changed saved configuration invalidates it, including external edits.
        if self._cache_key[0] != self.config_manager.config_fingerprint(config):
            return None
        return deepcopy(self._cache)

    def invalidate_cache(self) -> None:
        self._cache = None
        self._cache_key = None
        self._generation += 1

    async def run_smart(
        self, model: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("分析服务已关闭")
        config = await asyncio.to_thread(self.config_manager.reload_config)
        if self._closed:
            raise RuntimeError("分析服务已关闭")
        key = self._key(config, model)
        if not force and key == self._cache_key and self._cache is not None:
            return deepcopy(self._cache)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._execute(config, model, key), name="smart-analysis"
            )
            self._inflight[key] = task
            task.add_done_callback(lambda completed: self._finished(key, completed))
        # Cancelling one HTTP request must not cancel work shared by other callers.
        return deepcopy(await asyncio.shield(task))

    def _finished(self, key: tuple, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        # Retrieve failures even if all request waiters disconnected.
        if not task.cancelled():
            task.exception()

    async def _execute(
        self, config: LLMConfig, model: str | None, key: tuple
    ) -> dict[str, Any]:
        provider = self._provider_factory(config, model, self.settings)
        try:
            report = await provider.smart_analyze()
            result = asdict(report)
            result["model_used"] = self._model_identity(config, provider, model)[1]
        finally:
            await provider.close()
        # Persist inside the shared task, once per supplier call. Failed work or
        # failed storage must not be published as a successful cached analysis.
        if self._database is not None:
            await self._save_report(
                self._database,
                "smart",
                config,
                provider,
                result,
                model=model,
                input_summary=(
                    "黄金市场智能分析；联网搜索："
                    + ("是" if report.web_search_used else "否")
                ),
            )
        # An older job may finish after a config update; never publish its
        # result as the cache associated with the new configuration.
        current = await asyncio.to_thread(self.config_manager.reload_config)
        if key == self._key(current, model) and not self._closed:
            self._cache = deepcopy(result)
            self._cache_key = key
        return result

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._inflight.values()) + list(self._local_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._local_tasks.clear()

    async def run_local(self, database):
        if self._closed:
            raise RuntimeError("分析服务已关闭")
        task = asyncio.create_task(self._run_local(database), name="local-analysis")
        self._local_tasks.add(task)
        task.add_done_callback(self._local_tasks.discard)
        return await task

    async def _run_local(self, database):
        """Build the local volatility window and use the same supplier factory."""
        from ..analyzer import GoldAnalyzer

        end = utcnow()
        window_minutes = self.settings.alert_volatility_window
        records = await database.run(
            database.get_prices_in_range,
            end - timedelta(minutes=window_minutes),
            end,
            limit=1000,
            newest_first=True,
            currency="USD",
        )
        records.reverse()
        if len(records) < 2:
            records = list(
                reversed(
                    await database.run(
                        database.get_recent_prices, limit=20, currency="USD"
                    )
                )
            )
            if len(records) < 2:
                raise AnalysisDataError("数据不足，无法分析")
            window_minutes = max(
                1,
                int(
                    (records[-1].timestamp - records[0].timestamp).total_seconds() / 60
                ),
            )
        if records[-1].price <= 0:
            raise AnalysisDataError("当前价格必须大于0")
        config = await asyncio.to_thread(self.config_manager.reload_config)
        provider = self._provider_factory(config, None, self.settings)
        try:
            report = await GoldAnalyzer(llm_provider=provider).analyze_volatility(
                current_price=records[-1].price,
                price_change=records[-1].price - records[0].price,
                recent_prices=[(item.timestamp, item.price) for item in records],
                time_window_minutes=window_minutes,
            )
        finally:
            await provider.close()
        await self._save_report(
            database,
            "volatility",
            config,
            provider,
            asdict(report),
            price_range_start=records[0].timestamp,
            price_range_end=records[-1].timestamp,
            input_summary=(
                f"USD/oz；样本数：{len(records)}；窗口：{window_minutes} 分钟；"
                f"首价：{records[0].price}；末价：{records[-1].price}"
            ),
        )
        return report

    @staticmethod
    def _history_response(record) -> dict[str, Any]:
        return {
            "id": record.id,
            "analysis_type": record.analysis_type,
            "model_provider": record.model_provider,
            "model_name": record.model_name,
            "price_range_start": iso_utc(record.price_range_start)
            if record.price_range_start
            else None,
            "price_range_end": iso_utc(record.price_range_end)
            if record.price_range_end
            else None,
            "input_summary": record.input_summary,
            "result": record.get_result(),
            "created_at": iso_utc(record.created_at) if record.created_at else None,
        }

    async def history(
        self, database, limit: int = 50, analysis_type: str | None = None
    ) -> list[dict[str, Any]]:
        records = await database.run(
            database.get_analysis_records, limit=limit, analysis_type=analysis_type
        )
        return [self._history_response(record) for record in records]

    async def history_record(self, database, record_id: int) -> dict[str, Any] | None:
        record = await database.run(database.get_analysis_record, record_id)
        return self._history_response(record) if record is not None else None
