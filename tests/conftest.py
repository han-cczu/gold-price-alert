"""Isolated configuration, application lifespans and offline tests."""

import os
from pathlib import Path
import socket
import tempfile

import pytest

_TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp_pytest"
_TEST_ROOT.mkdir(exist_ok=True)
_SESSION_DIR = tempfile.TemporaryDirectory(
    prefix="pytest-session-", dir=_TEST_ROOT, ignore_cleanup_errors=True
)
for key, value in {
    "GOLD_DATA_SOURCE": "mock",
    "GOLD_LLM_PROVIDER": "mock",
    "GOLD_DATABASE_URL": f"sqlite:///{Path(_SESSION_DIR.name).as_posix()}/test.db",
    "GOLD_LLM_CONFIG_PATH": str(Path(_SESSION_DIR.name) / "llm_config.json"),
    "GOLD_ENCRYPT_API_KEYS": "false",
    "GOLD_ENABLE_AUTH": "false",
    "GOLD_SECRET_KEY": "",
    "GOLD_ADMIN_API_KEY": "test-admin",
    "GOLD_GOLDAPI_KEY": "",
    "GOLD_ANTHROPIC_API_KEY": "",
    "GOLD_OPENAI_API_KEY": "",
    "GOLD_TAVILY_API_KEY": "",
    "GOLD_SMTP_HOST": "",
    "GOLD_SMTP_USERNAME": "",
    "GOLD_SMTP_PASSWORD": "",
    "GOLD_ALERT_EMAIL_TO": "",
    "GOLD_WEBHOOK_URL": "",
    "GOLD_TELEGRAM_BOT_TOKEN": "",
    "GOLD_TELEGRAM_CHAT_ID": "",
}.items():
    os.environ[key] = value


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.getaddrinfo
    attempts = []

    def resolve(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1", b"localhost"):
            attempts.append(host)
            raise socket.gaierror("External network disabled in tests")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield
    assert not attempts, "External network attempted; inject a fake adapter"


@pytest.fixture
def app_settings(tmp_path):
    from gold_monitor.config import Settings

    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        llm_config_path=str(tmp_path / "llm.json"),
        backup_path=str(tmp_path / "backups"),
        data_source="mock",
        llm_provider="mock",
        enable_auth=False,
        encrypt_api_keys=False,
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def application(app_settings):
    from gold_monitor.web import create_app

    return create_app(app_settings, background_tasks=False)


@pytest.fixture
def client(application):
    from fastapi.testclient import TestClient

    with TestClient(application) as result:
        for i in range(5):
            application.state.runtime.db.save_price(2000 + i * 10, "mock")
        yield result
