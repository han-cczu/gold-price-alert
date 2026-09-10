"""Application analysis cache and task ownership; all suppliers are local fakes."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from gold_monitor.analysis.factory import provider_from_config
from gold_monitor.analysis.parser import parse_smart_response
from gold_monitor.analysis.providers import LLMProvider, OpenAIProvider
from gold_monitor.config import Settings
from gold_monitor.llm_config import LLMConfigManager
from gold_monitor.services.analysis import AnalysisService
from gold_monitor.time_utils import utcnow


FIXED_REPORT = """### 📊 市场概况
市场走势平稳，暂不建议追涨。
### 📈 近期走势
价格在固定区间波动。
### 🔍 影响因素
- 美元走势偏弱
- 风险偏好下降
### 🔮 价格预测
仅为固定测试样本。
### ⏰ 买入时机
等待回调。
### 💡 操作建议
建议控制风险。
### ⚠️ 风险提示
注意风险，以上不是实时行情。
"""


class ControlledProvider(LLMProvider):
    def __init__(self, model, gate=None, fail=False):
        self.model = model
        self.gate = gate
        self.started = asyncio.Event()
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def analyze(self, context):
        raise NotImplementedError

    async def smart_analyze(self):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("fake provider failure")
        report = parse_smart_response(FIXED_REPORT)
        report.web_search_used = True
        report.sources = [{"url": "https://example.invalid/source", "title": "fixture"}]
        return report

    async def close(self):
        self.closed = True


@pytest.fixture
def manager(tmp_path):
    manager = LLMConfigManager(tmp_path / "analysis.json", encrypt_keys=False)
    manager.set_active("mock", "model-v1")
    return manager


@pytest.mark.asyncio
async def test_same_concurrent_requests_share_one_call_and_cached_copy(manager):
    gate = asyncio.Event()
    provider = ControlledProvider("model-v1", gate)
    constructions = []

    def factory(config, model, settings):
        constructions.append(config.active_model)
        return provider

    service = AnalysisService(manager, provider_factory=factory)
    tasks = [asyncio.create_task(service.run_smart()) for _ in range(6)]
    await provider.started.wait()
    gate.set()
    reports = await asyncio.gather(*tasks)
    assert constructions == ["model-v1"]
    assert provider.calls == 1
    assert provider.closed
    assert all(report["sources"] for report in reports)
    reports[0]["sources"].clear()
    assert (await service.run_smart())["sources"]
    assert constructions == ["model-v1"]
    await service.close()


@pytest.mark.asyncio
async def test_config_change_invalidates_cache_and_force_refreshes(manager):
    providers = []

    def factory(config, model, settings):
        provider = ControlledProvider(model or config.active_model)
        providers.append(provider)
        return provider

    service = AnalysisService(manager, provider_factory=factory)
    await service.run_smart()
    manager.set_active("mock", "model-v2")
    assert service.get_cache() is None
    assert (await service.run_smart())["model_used"] == "model-v2"
    await service.run_smart(force=True)
    assert len(providers) == 3
    await service.close()


@pytest.mark.asyncio
async def test_old_inflight_result_cannot_replace_new_configuration_cache(manager):
    gate = asyncio.Event()
    old = ControlledProvider("model-v1", gate)
    new = ControlledProvider("model-v2")
    service = AnalysisService(
        manager,
        provider_factory=lambda config, *_: (
            old if config.active_model == "model-v1" else new
        ),
    )
    old_task = asyncio.create_task(service.run_smart())
    await old.started.wait()
    manager.set_active("mock", "model-v2")
    await service.run_smart()
    gate.set()
    assert (await old_task)["model_used"] == "model-v1"
    assert service.get_cache()["model_used"] == "model-v2"
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_supplier(manager):
    gate = asyncio.Event()
    provider = ControlledProvider("v1", gate)
    service = AnalysisService(manager, provider_factory=lambda *_: provider)
    cancelled = asyncio.create_task(service.run_smart())
    await provider.started.wait()
    survivor = asyncio.create_task(service.run_smart())
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    gate.set()
    assert (await survivor)["model_used"] == "v1"
    assert provider.calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_and_closes_supplier(manager):
    provider = ControlledProvider("v1", asyncio.Event())
    service = AnalysisService(manager, provider_factory=lambda *_: provider)
    task = asyncio.create_task(service.run_smart())
    await provider.started.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed
    with pytest.raises(RuntimeError, match="已关闭"):
        await service.run_smart()


@pytest.mark.asyncio
async def test_supplier_failure_is_not_cached_and_can_retry(manager):
    failed = ControlledProvider("v1", fail=True)
    service = AnalysisService(manager, provider_factory=lambda *_: failed)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fake provider failure"):
            await service.run_smart()
    assert failed.calls == 2
    assert service.get_cache() is None
    assert failed.closed
    await service.close()


def test_parser_keeps_keywords_in_body_and_sources_are_supplied_separately():
    report = parse_smart_response(FIXED_REPORT)
    assert report.market_overview == "市场走势平稳，暂不建议追涨。"
    assert report.key_factors == ["美元走势偏弱", "风险偏好下降"]
    assert report.recommendation == "建议控制风险。"
    assert report.risk_warning == "注意风险，以上不是实时行情。"
    assert report.sources == []
    assert report.raw_response == FIXED_REPORT


def test_factory_uses_injected_search_settings_and_cached_model(manager):
    manager.update_provider("deepseek", api_key="fake-key", models=["test-model"])
    manager.set_active("deepseek")
    settings = Settings(_env_file=None, tavily_api_key="fake-app-search-key")
    provider = provider_from_config(manager.get_config(), None, settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "test-model"
    assert provider._tavily_api_key == "fake-app-search-key"


@pytest.mark.asyncio
async def test_close_cancels_local_analysis_and_closes_supplier(manager):
    class LocalProvider(ControlledProvider):
        async def analyze(self, context):
            self.started.set()
            await asyncio.Event().wait()

    class Database:
        async def run(self, function, *args, **kwargs):
            return function(*args, **kwargs)

        def get_prices_in_range(self, *args, **kwargs):
            now = utcnow()
            return [
                SimpleNamespace(price=2001, timestamp=now),
                SimpleNamespace(price=2000, timestamp=now - timedelta(minutes=1)),
            ]

    provider = LocalProvider("fixture")
    service = AnalysisService(manager, provider_factory=lambda *_: provider)
    task = asyncio.create_task(service.run_local(Database()))
    await provider.started.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed
