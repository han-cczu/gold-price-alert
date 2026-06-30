"""安全模块测试 - 加密与鉴权"""

import pytest
from fastapi import HTTPException

from gold_monitor.config import settings
from gold_monitor.security import SecretManager, APIKeyAuth, is_admin_path
from gold_monitor.state import require_admin_dep
from gold_monitor.web import app


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

    assert SecretManager(secret_key="settings-secret").decrypt(ciphertext) == "stored-key"


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


def test_llm_write_endpoints_have_route_level_admin_dependency():
    """LLM 写接口应由路由依赖自身保护，不只依赖中间件前缀。"""
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    llm_write_routes = [
        route for route in app.routes
        if getattr(route, "path", "").startswith("/api/llm")
        and write_methods.intersection(getattr(route, "methods", set()))
    ]

    assert llm_write_routes
    for route in llm_write_routes:
        dependencies = getattr(route, "dependant").dependencies
        dependency_calls = {dependency.call for dependency in dependencies}
        assert require_admin_dep in dependency_calls, route.path
