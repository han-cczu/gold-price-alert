"""安全模块测试 - 加密与鉴权"""

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from gold_monitor.config import settings
from gold_monitor.security import (
    SecretManager,
    APIKeyAuth,
    RateLimiter,
    is_admin_path,
    is_rate_limited_path,
)
from gold_monitor.state import require_admin_dep
from gold_monitor.web import app, create_app


# ============ Fernet 加密 ============


def test_fernet_roundtrip():
    sm = SecretManager(secret_key="master-key")
    ct = sm.encrypt("sk-abc-123")
    assert ct and ct != "sk-abc-123"
    assert sm.decrypt(ct) == "sk-abc-123"


def test_encrypt_empty_returns_empty():
    sm = SecretManager(secret_key="master-key")
    assert sm.encrypt("") == ""
    assert sm.decrypt("") == ""


def test_decrypt_invalid_degrades_to_empty():
    """无效密文（含旧 XOR 格式）安全降级为空串，不抛异常"""
    sm = SecretManager(secret_key="master-key")
    assert sm.decrypt("not-a-valid-token") == ""
    assert sm.decrypt("YWJjZGVmZ2g=") == ""


def test_decrypt_with_wrong_key_returns_empty():
    ct = SecretManager(secret_key="key-a").encrypt("secret")
    assert SecretManager(secret_key="key-b").decrypt(ct) == ""


def test_secret_manager_defaults_to_settings_secret_key(monkeypatch):
    """从 .env 加载到 settings 的 GOLD_SECRET_KEY 应作为默认主密钥。"""
    monkeypatch.delenv("GOLD_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "secret_key", "settings-secret")

    ciphertext = SecretManager().encrypt("stored-key")

    assert (
        SecretManager(secret_key="settings-secret").decrypt(ciphertext) == "stored-key"
    )


# ============ 管理路径覆盖 ============


def test_admin_paths_cover_previously_missed_endpoints():
    assert is_admin_path("/api/data/export")
    assert is_admin_path("/api/data/backups")
    assert is_admin_path("/api/data/stats")
    assert is_admin_path("/api/notifications/logs")
    assert is_admin_path("/api/llm/providers")
    assert not is_admin_path("/api/price/current")
    assert not is_admin_path("/health")


def test_api_key_auth_defaults_to_settings_admin_key(monkeypatch):
    """从 .env 加载到 settings 的 GOLD_ADMIN_API_KEY 应作为默认管理密钥。"""
    monkeypatch.delenv("GOLD_ADMIN_API_KEY", raising=False)
    monkeypatch.setattr(settings, "admin_api_key", "settings-admin")

    auth = APIKeyAuth()

    assert auth.admin_key == "settings-admin"


def test_admin_key_is_only_accepted_from_header():
    """管理密钥不应支持 query 参数，避免进入日志/浏览器历史。"""
    auth = APIKeyAuth(admin_api_key="secret")

    assert auth.verify_admin_key(FakeRequest(headers={"X-Admin-Key": "secret"}))
    assert not auth.verify_admin_key(FakeRequest(query={"admin_key": "secret"}))


def test_security_status_does_not_expose_admin_key_hint():
    """安全状态接口不应泄露管理密钥前缀。"""
    response = TestClient(app).get("/api/security/status")

    assert response.status_code == 200
    assert "admin_key_hint" not in response.json()


# ============ require_admin 鉴权 ============


class FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


@pytest.mark.asyncio
async def test_require_admin_disabled_allows(monkeypatch):
    """enable_auth=False（默认）时放行，不破坏本地开发"""
    monkeypatch.setattr(settings, "enable_auth", False)
    auth = APIKeyAuth(admin_api_key="secret")
    assert await auth.require_admin(FakeRequest()) is True


@pytest.mark.asyncio
async def test_require_admin_enabled_blocks_without_key(monkeypatch):
    monkeypatch.setattr(settings, "enable_auth", True)
    auth = APIKeyAuth(admin_api_key="secret")
    with pytest.raises(HTTPException) as exc:
        await auth.require_admin(FakeRequest())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_enabled_allows_with_correct_key(monkeypatch):
    monkeypatch.setattr(settings, "enable_auth", True)
    auth = APIKeyAuth(admin_api_key="secret")
    req = FakeRequest(headers={"X-Admin-Key": "secret"})
    assert await auth.require_admin(req) is True


@pytest.mark.asyncio
async def test_require_admin_enabled_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "enable_auth", True)
    auth = APIKeyAuth(admin_api_key="secret")
    req = FakeRequest(headers={"X-Admin-Key": "wrong"})
    with pytest.raises(HTTPException):
        await auth.require_admin(req)


def _iter_api_routes(routes):
    """兼容 FastAPI 新旧版本的 router include 内部结构。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route

        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _iter_api_routes(getattr(original_router, "routes", ()))

        nested_routes = getattr(route, "routes", None)
        if nested_routes:
            yield from _iter_api_routes(nested_routes)


def test_llm_write_endpoints_have_route_level_admin_dependency():
    """LLM 写接口应由路由依赖自身保护，不只依赖中间件前缀。"""
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    llm_write_routes = [
        route
        for route in _iter_api_routes(app.routes)
        if getattr(route, "path", "").startswith("/api/llm")
        and write_methods.intersection(getattr(route, "methods", set()))
    ]

    assert llm_write_routes
    for route in llm_write_routes:
        dependencies = getattr(route, "dependant").dependencies
        dependency_calls = {dependency.call for dependency in dependencies}
        assert require_admin_dep in dependency_calls, route.path


def test_admin_path_routes_have_route_level_admin_dependency():
    """敏感命名空间应由路由依赖自身保护，不只依赖中间件前缀。"""
    admin_routes = [
        route
        for route in _iter_api_routes(app.routes)
        if is_admin_path(getattr(route, "path", ""))
    ]

    assert admin_routes
    for route in admin_routes:
        dependencies = getattr(route, "dependant").dependencies
        dependency_calls = {dependency.call for dependency in dependencies}
        assert require_admin_dep in dependency_calls, route.path


def _peer_request(host, forwarded=""):
    return Request(
        {
            "type": "http",
            "client": (host, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        }
    )


def test_rate_limit_uses_peer_not_untrusted_forwarded_header():
    limiter = RateLimiter(1, clock=lambda: 1.0)
    assert limiter.is_allowed(_peer_request("client-a", "203.0.113.1"))[0]
    for forged in ("203.0.113.2", "203.0.113.3, 127.0.0.1", "", "arbitrary-id"):
        assert not limiter.is_allowed(_peer_request("client-a", forged))[0]
    assert limiter.is_allowed(_peer_request("client-b", "203.0.113.1"))[0]


def test_rate_limit_reclaims_all_expired_identities():
    now = [0.0]
    limiter = RateLimiter(1, clock=lambda: now[0])
    for index in range(1000):
        assert limiter.is_allowed(_peer_request(f"client-{index}"))[0]
    now[0] = 600.0
    assert limiter.is_allowed(_peer_request("new-client"))[0]
    assert list(limiter._requests) == ["new-client"]


def test_rate_limit_capacity_does_not_evict_live_quotas():
    now = [0.0]
    limiter = RateLimiter(1, max_clients=2, clock=lambda: now[0])
    assert limiter.is_allowed(_peer_request("a"))[0]
    now[0] = 1.0
    assert limiter.is_allowed(_peer_request("b"))[0]
    assert not limiter.is_allowed(_peer_request("c"))[0]
    assert not limiter.is_allowed(_peer_request("a"))[0]
    assert len(limiter._requests) == 2
    now[0] = 60.0
    assert limiter.is_allowed(_peer_request("c"))[0]
    assert list(limiter._requests) == ["b", "c"]


def test_rate_limit_sliding_window_preserves_recent_samples():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    peer = _peer_request("a")
    assert limiter.is_allowed(peer) == (True, 1)
    now[0] = 30.0
    assert limiter.is_allowed(peer) == (True, 0)
    now[0] = 60.0
    assert limiter.is_allowed(peer) == (True, 0)
    assert limiter.is_allowed(peer) == (False, 0)


def test_every_api_path_is_rate_limited_but_probes_and_assets_are_not():
    for path in (
        "/api/chart/data",
        "/api/price/current",
        "/api/collector/gaps",
        "/api/analysis",
        "/api/llm/config",
    ):
        assert is_rate_limited_path(path), path
    for path in ("/health", "/metrics", "/", "/static/js/dashboard.js", "/ws/status"):
        assert not is_rate_limited_path(path), path


def test_public_chart_requests_are_rate_limited_per_client(app_settings):
    """匿名图表聚合请求必须受每客户端配额约束，不能无限占用数据库线程。"""
    limited = create_app(
        app_settings.model_copy(update={"rate_limit_per_minute": 3}),
        background_tasks=False,
    )
    with TestClient(limited) as client:
        statuses = [
            client.get("/api/chart/data?hours=43800").status_code for _ in range(5)
        ]
        assert statuses == [200, 200, 200, 429, 429]
        rejected = client.get("/api/price/latest")
        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "60"
        assert client.get("/health").status_code != 429


def test_provider_trigger_routes_have_admin_dependency():
    targets = {("GET", "/api/analysis"), ("POST", "/api/smart-analysis/refresh")}
    found = set()
    for route in _iter_api_routes(app.routes):
        for method in route.methods:
            key = (method, route.path)
            if key in targets:
                found.add(key)
                assert require_admin_dep in {
                    dependency.call for dependency in route.dependant.dependencies
                }
    assert found == targets
