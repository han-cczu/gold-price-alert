"""Application isolation, lifecycle and realtime delivery regressions."""

import asyncio
from dataclasses import asdict
from datetime import timedelta
import os
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from gold_monitor.analysis.providers import MockLLMProvider
from gold_monitor.models import Database
from gold_monitor.realtime import ConnectionManager
from gold_monitor.runtime import ApplicationRuntime
from gold_monitor.time_utils import iso_utc, utcnow
from gold_monitor.web import create_app


def test_import_does_not_create_database(tmp_path):
    path = tmp_path / "must-not-exist.db"
    env = dict(os.environ, GOLD_DATABASE_URL=f"sqlite:///{path.as_posix()}")
    subprocess.run(
        [sys.executable, "-c", "import gold_monitor.web; import gold_monitor.state"],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert not path.exists()


def test_application_instances_isolate_database_and_auth(app_settings, tmp_path):
    first = create_app(
        app_settings.model_copy(update={"enable_auth": True, "admin_api_key": "first"}),
        background_tasks=False,
    )
    second = create_app(
        app_settings.model_copy(
            update={
                "database_url": f"sqlite:///{(tmp_path / 'second.db').as_posix()}",
                "enable_auth": True,
                "admin_api_key": "second",
            }
        ),
        background_tasks=False,
    )
    with TestClient(first) as a, TestClient(second) as b:
        first.state.runtime.db.save_price(3000, "first")
        assert a.get("/api/price/latest").json()["price"] == 3000
        assert b.get("/api/price/latest").status_code == 404
        assert (
            a.get("/api/llm/config", headers={"X-Admin-Key": "first"}).status_code
            == 200
        )
        assert (
            b.get("/api/llm/config", headers={"X-Admin-Key": "first"}).status_code
            == 401
        )
        assert first.state.runtime.ws is not second.state.runtime.ws
    assert first.state.runtime.database is None
    assert second.state.runtime.database is None


def test_startup_failure_releases_already_created_resources(app_settings):
    db = Database(app_settings.database_url)

    def broken_market(config):
        raise RuntimeError("startup failure")

    app = create_app(app_settings, database=db, market_factory=broken_market)
    with pytest.raises(RuntimeError, match="startup failure"):
        with TestClient(app):
            pass
    assert app.state.runtime.database is None
    assert not app.state.runtime.started
    assert not app.state.runtime.tasks


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_tasks_and_continues_after_cleanup_error(
    app_settings,
):
    runtime = ApplicationRuntime(app_settings, background_tasks=False)
    await runtime.start()
    task = runtime.spawn(asyncio.sleep(100))
    client = runtime.market._client

    async def broken_bank_close():
        raise RuntimeError("close failure")

    runtime.market._bank.close = broken_bank_close
    await runtime.close()
    assert task.cancelled()
    assert not runtime.tasks
    assert runtime.database is None
    # An individual adapter close failure must not leave its HTTP client open.
    assert client.is_closed


def test_real_lifespan_starts_and_stops_background_collectors(app_settings):
    app = create_app(app_settings)
    with TestClient(app) as client:
        runtime = app.state.runtime
        assert client.get("/api/price/current").status_code == 200
        assert client.get("/health").json()["collector_running"] is True
        collector = runtime.collector
    assert not collector.is_running
    assert not runtime.tasks
    assert runtime.database is None


@pytest.mark.parametrize("report_age_hours, expected_provider_calls", [(1, 0), (30, 1)])
def test_startup_cleans_expired_prices_and_reuses_a_recent_stored_report(
    app_settings, monkeypatch, report_age_hours, expected_provider_calls
):
    """启动即执行保留期清理；24 小时内的已存报告直接复用，不再付费重跑。"""
    database = Database(app_settings.database_url)
    database.create_tables()
    now = utcnow()
    database.save_price(
        1999,
        "old",
        timestamp=now - timedelta(days=app_settings.data_retention_days + 1),
    )
    database.save_price(2001, "recent", timestamp=now)
    stored = asdict(asyncio.run(MockLLMProvider().smart_analyze()))
    stored["market_overview"] = "stored before restart"
    stored["generated_at"] = iso_utc(now - timedelta(hours=report_age_hours))
    stored["model_used"] = "Mock"
    database.save_analysis_record(
        "smart", model_provider="mock", model_name="Mock", result=stored
    )
    database.close()
    calls = []
    original = MockLLMProvider.smart_analyze

    async def counted(provider):
        calls.append(1)
        return await original(provider)

    monkeypatch.setattr(MockLLMProvider, "smart_analyze", counted)
    app = create_app(app_settings)
    with TestClient(app) as client:
        runtime = app.state.runtime
        deadline = time.monotonic() + 5

        def expired_rows():
            # The running Mock collector keeps adding fresh rows; only the
            # sample older than the retention period must disappear.
            return runtime.db.get_prices_in_range(
                now - timedelta(days=400),
                now - timedelta(days=app_settings.data_retention_days),
                limit=10,
            )

        def settled():
            return not expired_rows() and runtime.analysis.get_cache() is not None

        while time.monotonic() < deadline and not settled():
            time.sleep(0.05)
        assert expired_rows() == [], "expired sample removed at startup"
        assert runtime.db.get_latest_price().price >= 2001
        cache = runtime.analysis.get_cache()
        assert cache is not None, "startup published a smart analysis"
        report = client.get("/api/smart-analysis").json()
        assert report["is_cached"] is True
        if expected_provider_calls == 0:
            assert cache["market_overview"] == "stored before restart"
            assert report["market_overview"] == "stored before restart"
            assert report["cache_age_minutes"] >= 59
        else:
            assert report["market_overview"] != "stored before restart"
    assert len(calls) == expected_provider_calls


class FakeSocket:
    def __init__(self, slow=False):
        self.slow = slow
        self.messages = []
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, message):
        if self.slow:
            await asyncio.sleep(100)
        self.messages.append(message)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_slow_websocket_does_not_block_other_clients():
    manager = ConnectionManager(send_timeout=0.02)
    slow, fast = FakeSocket(True), FakeSocket()
    await manager.connect(slow)
    await manager.connect(fast)
    await asyncio.wait_for(manager.broadcast({"type": "price_update"}), timeout=0.1)
    await asyncio.sleep(0.04)
    assert fast.messages == [{"type": "price_update"}]
    assert slow.closed
    await manager.close()
    assert fast.closed
    assert manager.connection_count == 0
