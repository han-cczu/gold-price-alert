"""Offline regressions for paid analysis authorization and durable reports."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gold_monitor.analysis.providers import MockLLMProvider
from gold_monitor.llm_config import LLMConfigManager
from gold_monitor.models import Database
from gold_monitor.services.analysis import AnalysisService
from gold_monitor.time_utils import utcnow
from gold_monitor.web import create_app


def test_anonymous_reads_cache_but_cannot_start_provider(app_settings, monkeypatch):
    app_settings.enable_auth = True
    app_settings.admin_api_key = "fixture-admin"
    calls = []
    original_smart = MockLLMProvider.smart_analyze
    original_local = MockLLMProvider.analyze

    async def smart(provider):
        calls.append("smart")
        return await original_smart(provider)

    async def local(provider, context):
        calls.append("local")
        return await original_local(provider, context)

    monkeypatch.setattr(MockLLMProvider, "smart_analyze", smart)
    monkeypatch.setattr(MockLLMProvider, "analyze", local)
    app = create_app(app_settings, background_tasks=False)
    headers = {"X-Admin-Key": "fixture-admin"}
    with TestClient(app) as client:
        for auth in ({}, {"X-Admin-Key": "wrong"}):
            assert client.get("/api/smart-analysis", headers=auth).status_code == 401
            assert client.get("/api/analysis", headers=auth).status_code == 401
            assert (
                client.post(
                    "/api/smart-analysis/refresh",
                    headers=auth,
                    json={"model": "fixture-model"},
                ).status_code
                == 401
            )
        assert calls == []
        assert client.get("/api/smart-analysis", headers=headers).status_code == 200
        public = client.get("/api/smart-analysis")
        assert public.status_code == 200
        assert public.json()["is_cached"] is True
        assert calls == ["smart"]
        assert (
            client.post("/api/smart-analysis/refresh", headers=headers).status_code
            == 200
        )
        now = utcnow()
        app.state.runtime.db.save_price(
            2000, "fixture", timestamp=now - timedelta(minutes=2)
        )
        app.state.runtime.db.save_price(
            2010, "fixture", timestamp=now - timedelta(minutes=1)
        )
        assert client.get("/api/analysis", headers=headers).status_code == 200
        assert calls == ["smart", "smart", "local"]
        records = client.get("/api/analysis/history").json()["records"]
        assert len(records) == 3
        assert sorted(record["analysis_type"] for record in records) == [
            "smart",
            "smart",
            "volatility",
        ]
        assert all(record["result"]["generated_at"].endswith("Z") for record in records)
        app.state.runtime.llm_config.set_active("mock", "changed-model")
        assert client.get("/api/smart-analysis").status_code == 401
        assert calls == ["smart", "smart", "local"]


@pytest.fixture
async def history_database(app_settings):
    database = Database(app_settings.database_url)
    await database.run(database.create_tables)
    yield database
    await database.aclose()


@pytest.mark.asyncio
async def test_shared_smart_analysis_persists_once_and_survives_restart(
    app_settings, history_database
):
    started, gate = asyncio.Event(), asyncio.Event()

    class Provider(MockLLMProvider):
        calls = 0

        async def smart_analyze(self):
            self.calls += 1
            started.set()
            await gate.wait()
            report = await super().smart_analyze()
            report.sources = [
                {"url": "https://example.invalid/gold", "title": "fixture"}
            ]
            report.web_search_used = True
            return report

    provider = Provider()
    manager = LLMConfigManager(settings=app_settings)
    service = AnalysisService(
        manager, database=history_database, provider_factory=lambda *_: provider
    )
    tasks = [asyncio.create_task(service.run_smart()) for _ in range(6)]
    await started.wait()
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    gate.set()
    await asyncio.gather(*tasks[1:])
    assert provider.calls == 1
    await service.run_smart()
    records = await history_database.run(history_database.get_analysis_records)
    assert len(records) == 1
    assert records[0].model_provider == "mock"
    assert records[0].model_name == "Mock"
    assert records[0].get_result()["sources"][0]["title"] == "fixture"
    assert records[0].get_result()["web_search_used"] is True
    await service.run_smart(force=True)
    assert provider.calls == 2
    await service.close()
    await history_database.aclose()
    reopened = Database(app_settings.database_url)
    try:
        assert len(await reopened.run(reopened.get_analysis_records)) == 2
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["smart", "local"])
async def test_failed_provider_does_not_create_history(
    app_settings, history_database, kind
):
    class Provider(MockLLMProvider):
        async def smart_analyze(self):
            raise RuntimeError("fixture provider error")

        async def analyze(self, context):
            raise RuntimeError("fixture provider error")

    service = AnalysisService(
        LLMConfigManager(settings=app_settings),
        database=history_database,
        provider_factory=lambda *_: Provider(),
    )
    now = utcnow()
    for minutes, price in [(2, 2000), (1, 2010)]:
        await history_database.run(
            history_database.save_price,
            price,
            "fixture",
            timestamp=now - timedelta(minutes=minutes),
        )
    with pytest.raises(RuntimeError, match="fixture provider error"):
        if kind == "smart":
            await service.run_smart()
        else:
            await service.run_local(history_database)
    assert await history_database.run(history_database.get_analysis_records) == []
    assert service.get_cache() is None
    await service.close()


@pytest.mark.asyncio
async def test_storage_failure_does_not_publish_smart_cache(
    app_settings, history_database, monkeypatch
):
    def fail(*args, **kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(history_database, "save_analysis_record", fail)
    service = AnalysisService(
        LLMConfigManager(settings=app_settings), database=history_database
    )
    with pytest.raises(OSError, match="fixture disk failure"):
        await service.run_smart()
    assert service.get_cache() is None
    assert await history_database.run(history_database.get_analysis_records) == []
    await service.close()


@pytest.mark.asyncio
async def test_local_history_records_usd_window_and_actual_model(
    app_settings, history_database
):
    now = utcnow()
    for minutes, price, currency in [
        (2, 2000, "USD"),
        (1, 2010, "USD"),
        (0, 9999, "CNY"),
    ]:
        await history_database.run(
            history_database.save_price,
            price,
            "fixture",
            currency=currency,
            timestamp=now - timedelta(minutes=minutes),
        )
    service = AnalysisService(LLMConfigManager(settings=app_settings))
    report = await service.run_local(history_database)
    records = await history_database.run(history_database.get_analysis_records)
    assert len(records) == 1
    record = records[0]
    assert record.analysis_type == "volatility"
    assert record.price_range_start == now - timedelta(minutes=2)
    assert record.price_range_end == now - timedelta(minutes=1)
    assert record.model_provider == "mock"
    assert record.model_name == "Mock"
    assert "USD/oz" in record.input_summary
    assert "样本数：2" in record.input_summary
    assert record.get_result()["summary"] == report.summary
    await service.close()
