"""Failure readiness and bounded metrics regressions; no external services."""

from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import pytest

from gold_monitor.collection.stats import CollectorStats
from gold_monitor.dependencies import get_runtime
from gold_monitor.healthcheck import check_health
from gold_monitor.metrics import (
    API_REQUEST_TOTAL,
    API_REQUEST_LATENCY,
    MetricsMiddleware,
)
from gold_monitor.routers.system import router
from gold_monitor.time_utils import utcnow
from gold_monitor.security import RateLimiter


@pytest.mark.parametrize(
    "failure", [None, "stopped", "database", "source", "stale", "never_received"]
)
def test_readiness_requires_database_running_collector_and_fresh_success(failure):
    now = utcnow()

    class Database:
        def get_latest_price(self):
            if failure == "database":
                raise RuntimeError("injected database failure")
            return SimpleNamespace(price=2300, timestamp=now)

        async def run(self, operation):
            return operation()

    stats = CollectorStats(last_success_at=now)
    if failure == "source":
        stats.consecutive_failures = 1
    if failure == "stale":
        stats.last_success_at = now - timedelta(minutes=10)
    if failure == "never_received":
        stats.last_success_at = None
    collector = SimpleNamespace(
        is_running=failure != "stopped",
        last_price=None if failure == "never_received" else SimpleNamespace(price=2300),
        stats=stats,
        get_config=lambda: {"interval": 30},
    )
    runtime = SimpleNamespace(
        db=Database(),
        collector=collector,
        started_at=now,
        config=SimpleNamespace(data_source="mock"),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_runtime] = lambda: runtime
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == (503 if failure else 200)
    assert response.json()["status"] == ("unhealthy" if failure else "healthy")


@pytest.mark.parametrize(
    "status_code, body",
    [
        (503, {"status": "unhealthy"}),
        (200, {"status": "unhealthy"}),
        (
            200,
            {
                "status": "healthy",
                "database": "connected",
                "collector_running": True,
                "data_source_healthy": False,
            },
        ),
    ],
)
def test_container_check_rejects_failed_or_incomplete_readiness(status_code, body):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body)
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            check_health(client)


def test_container_check_accepts_healthy_service():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "healthy",
                "database": "connected",
                "collector_running": True,
                "data_source_healthy": True,
            },
        )
    )
    with httpx.Client(transport=transport) as client:
        check_health(client)


def test_metrics_use_route_templates_and_one_unknown_route_label():
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/audit/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    before_counter = set(API_REQUEST_TOTAL._metrics)
    before_histogram = set(API_REQUEST_LATENCY._metrics)
    with TestClient(app) as client:
        for index in range(100):
            assert client.get(f"/audit/items/{index}").status_code == 200
            assert client.get(f"/audit/missing/{index}").status_code == 404
            assert (
                client.request(f"UNUSUAL{index}", f"/audit/missing/{index}").status_code
                == 404
            )
    new_counter = set(API_REQUEST_TOTAL._metrics) - before_counter
    new_histogram = set(API_REQUEST_LATENCY._metrics) - before_histogram
    assert len(new_counter) <= 3 and len(new_histogram) <= 3
    assert {key[1] for key in new_counter} <= {"/audit/items/{item_id}", "unmatched"}
    assert {key[0] for key in new_counter} <= {"GET", "OTHER"}


@pytest.mark.parametrize(
    "peer, expected", [("203.0.113.8", "203.0.113.8"), ("192.0.2.9", "198.51.100.5")]
)
async def test_limiter_uses_only_server_verified_proxy_identity(peer, expected):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = FastAPI()
    limiter = RateLimiter(requests_per_minute=1)

    @app.get("/probe")
    def probe(request: Request):
        return {
            "client": request.client.host,
            "allowed": limiter.is_allowed(request)[0],
        }

    server = ProxyHeadersMiddleware(app, trusted_hosts="192.0.2.9")
    transport = httpx.ASGITransport(app=server, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/probe", headers={"X-Forwarded-For": "spoofed-a, 198.51.100.5"}
        )
        second = await client.get(
            "/probe", headers={"X-Forwarded-For": "spoofed-b, 198.51.100.5"}
        )
    assert first.json() == {"client": expected, "allowed": True}
    assert second.json() == {"client": expected, "allowed": False}


def test_cli_passes_configured_trusted_proxy_addresses(monkeypatch):
    from gold_monitor.cli import run_server
    import uvicorn

    recorded = []
    monkeypatch.setenv("GOLD_TRUSTED_PROXY_IPS", "192.0.2.9")
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: recorded.append(kwargs))
    run_server(host="127.0.0.1", port=9876)
    assert recorded[0]["proxy_headers"] is True
    assert recorded[0]["forwarded_allow_ips"] == "192.0.2.9"


async def test_collection_delivery_and_database_metrics_are_recorded(app_settings):
    """指标记录函数必须真正接线，否则 /metrics 里的相关序列永远为空。"""
    from gold_monitor.alerts.types import Alert, AlertType
    from gold_monitor.metrics import (
        DB_OPERATION_LATENCY,
        FETCH_TOTAL,
        NOTIFICATION_TOTAL,
    )
    from gold_monitor.notifications.delivery import NotificationManager
    from gold_monitor.runtime import ApplicationRuntime

    class Delivered:
        async def send(self, alert):
            return True

    runtime = ApplicationRuntime(app_settings, background_tasks=False)
    await runtime.start()
    try:
        fetched = FETCH_TOTAL.labels(source="mock", status="success")._value.get()
        assert (await runtime.collector.collect()).price is not None
        assert FETCH_TOTAL.labels(source="mock", status="success")._value.get() == (
            fetched + 1
        )
        delivered = NOTIFICATION_TOTAL.labels(
            channel="Delivered", status="success"
        )._value.get()
        manager = NotificationManager(runtime.db, [Delivered()])
        await manager.send_with_retry(
            Alert(AlertType.VOLATILITY, 2000.0, "metrics", utcnow())
        )
        assert (
            NOTIFICATION_TOTAL.labels(
                channel="Delivered", status="success"
            )._value.get()
            == delivered + 1
        )
        assert ("save_notification_log",) in DB_OPERATION_LATENCY._metrics
    finally:
        await runtime.close()
