"""Analysis and model APIs use isolated application resources and fake suppliers."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gold_monitor.analysis.providers import MockLLMProvider
from gold_monitor.config import Settings
from gold_monitor.time_utils import utcnow
from gold_monitor.web import create_app


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        llm_config_path=str(tmp_path / "api-llm.json"),
        data_source="mock",
        llm_provider="mock",
        enable_auth=False,
        encrypt_api_keys=False,
        secret_key="",
        tavily_api_key="",
    )
    return create_app(settings, background_tasks=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def test_smart_cache_refresh_and_config_change(client, app, monkeypatch):
    calls = []
    original = MockLLMProvider.smart_analyze

    async def report(provider):
        calls.append(True)
        return await original(provider)

    monkeypatch.setattr(MockLLMProvider, "smart_analyze", report)
    first = client.get("/api/smart-analysis")
    assert first.status_code == 200
    assert first.json()["is_cached"] is False
    assert first.json()["generated_at"].endswith("Z")
    assert client.get("/api/smart-analysis").json()["is_cached"] is True
    refreshed = client.post(
        "/api/smart-analysis/refresh", json={"model": "fixture-model"}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["generated_at"].endswith("Z")
    assert len(calls) == 2
    assert (
        client.post(
            "/api/llm/active", json={"provider_id": "mock", "model": "new-model"}
        ).status_code
        == 200
    )
    assert client.get("/api/smart-analysis").json()["is_cached"] is False
    assert len(calls) == 3


def test_local_analysis_and_bounded_history(client, app):
    assert client.get("/api/analysis").status_code == 400
    db = app.state.runtime.db
    now = utcnow()
    db.save_price(price=2000, source="fixture", timestamp=now - timedelta(minutes=2))
    db.save_price(price=2010, source="fixture", timestamp=now - timedelta(minutes=1))
    report = client.get("/api/analysis")
    assert report.status_code == 200
    assert report.json()["generated_at"].endswith("Z")
    assert report.json()["market_sentiment"] == "偏多"
    record = db.save_analysis_record(
        "smart",
        model_provider="mock",
        result={"summary": "fixture"},
        price_range_start=now,
    )
    listing = client.get("/api/analysis/history?limit=1")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["records"][0]["price_range_start"].endswith("Z")
    detail = client.get(f"/api/analysis/history/{record.id}")
    assert detail.status_code == 200
    assert detail.json()["result"] == {"summary": "fixture"}
    assert client.get("/api/analysis/history/9999").status_code == 404
    assert client.get("/api/analysis/history?limit=0").status_code == 422


@pytest.mark.parametrize("fallback", [False, True])
def test_local_analysis_filters_currency_before_sql_limit(
    client, app, monkeypatch, fallback
):
    db = app.state.runtime.db
    now = utcnow()
    age = 15 if fallback else 2
    db.save_price(
        price=2000,
        currency="USD",
        source="fixture",
        timestamp=now - timedelta(minutes=age),
    )
    db.save_price(
        price=2010,
        currency="USD",
        source="fixture",
        timestamp=now - timedelta(minutes=age - 1),
    )
    # More recent foreign-currency rows outnumber both query bounds.
    with db.get_session() as session:
        from gold_monitor.models import GoldPrice

        session.add_all(
            [
                GoldPrice(
                    price=99999,
                    currency="CNY",
                    source="fixture",
                    timestamp=now - timedelta(seconds=1),
                )
                for _ in range(1001)
            ]
        )
        session.commit()
    contexts = []
    original = MockLLMProvider.analyze

    async def analyze(provider, context):
        contexts.append(context)
        return await original(provider, context)

    monkeypatch.setattr(MockLLMProvider, "analyze", analyze)
    response = client.get("/api/analysis")
    assert response.status_code == 200
    assert [price for _, price in contexts[0].recent_prices] == [2000, 2010]
    assert contexts[0].price_change == 10
    assert contexts[0].time_window_minutes == (
        1 if fallback else app.state.runtime.config.alert_volatility_window
    )


def test_local_analysis_uses_environment_supplier_only_without_saved_config(
    tmp_path, monkeypatch
):
    from gold_monitor.analysis.providers import OpenAIProvider

    settings = Settings(
        _env_file=None,
        data_source="mock",
        llm_provider="openai",
        openai_api_key="fake-env-key",
        anthropic_api_key="",
        enable_auth=False,
        database_url=f"sqlite:///{tmp_path / 'env-api.db'}",
        llm_config_path=str(tmp_path / "env-llm.json"),
        encrypt_api_keys=False,
    )
    application = create_app(settings, background_tasks=False)
    calls = []

    async def analyze(provider, context):
        calls.append(provider.api_key)
        return await MockLLMProvider().analyze(context)

    monkeypatch.setattr(OpenAIProvider, "analyze", analyze)
    with TestClient(application) as client:
        now = utcnow()
        application.state.runtime.db.save_price(
            2000, "fixture", timestamp=now - timedelta(minutes=2)
        )
        application.state.runtime.db.save_price(
            2010, "fixture", timestamp=now - timedelta(minutes=1)
        )
        assert client.get("/api/analysis").status_code == 200
        assert calls == ["fake-env-key"]
        assert (
            client.post("/api/llm/active", json={"provider_id": "mock"}).status_code
            == 200
        )
        assert client.get("/api/analysis").status_code == 200
        assert calls == ["fake-env-key"]


def test_model_mutations_preserve_masked_key_and_report_write_failure(
    client, app, monkeypatch
):
    response = client.put(
        "/api/llm/providers/openai", json={"api_key": "fake-long-secret-key"}
    )
    assert response.status_code == 200
    assert "fake-long-secret-key" not in response.text
    masked = response.json()["provider"]["api_key"]
    assert (
        client.put(
            "/api/llm/providers/openai", json={"api_key": masked, "name": "renamed"}
        ).status_code
        == 200
    )
    assert (
        app.state.runtime.llm_config.get_provider("openai").api_key
        == "fake-long-secret-key"
    )
    before = app.state.runtime.llm_config.config_path.read_bytes()

    def fail(*args):
        raise OSError("fake-long-secret-key should never escape")

    monkeypatch.setattr("gold_monitor.llm_config.os.replace", fail)
    response = client.put("/api/llm/providers/openai", json={"name": "failed"})
    assert response.status_code == 503
    assert "fake-long-secret-key" not in response.text
    assert app.state.runtime.llm_config.config_path.read_bytes() == before
    assert app.state.runtime.llm_config.get_provider("openai").name == "renamed"


def test_llm_route_requires_application_admin_key(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        llm_config_path=str(tmp_path / "auth.json"),
        data_source="mock",
        enable_auth=True,
        admin_api_key="fake-admin-key",
        encrypt_api_keys=False,
    )
    with TestClient(create_app(settings, background_tasks=False)) as client:
        assert client.get("/api/llm/config").status_code == 401
        assert (
            client.get("/api/llm/config", headers={"X-Admin-Key": "wrong"}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/llm/config", headers={"X-Admin-Key": "fake-admin-key"}
            ).status_code
            == 200
        )


def test_connection_probe_uses_unsaved_form_and_closes_client(client, app, monkeypatch):
    calls, closed = [], []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok fake-unsaved-key")
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

        async def close(self):
            closed.append(True)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)
    response = client.post(
        "/api/llm/providers/openai/test",
        json={
            "api_key": "fake-unsaved-key",
            "base_url": "https://example.invalid/custom",
            "model": "fixture-model",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert "fake-unsaved-key" not in response.text
    assert calls[0]["api_key"] == "fake-unsaved-key"
    assert calls[0]["base_url"] == "https://example.invalid/custom/v1"
    assert calls[1]["max_tokens"] == 50
    assert closed == [True]
    assert app.state.runtime.llm_config.get_provider("openai").api_key == ""


def test_probe_failure_does_not_echo_supplier_exception(client, monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            raise RuntimeError("Bearer fake-unsaved-key")

    monkeypatch.setattr("openai.AsyncOpenAI", BrokenClient)
    response = client.post(
        "/api/llm/providers/openai/test", json={"api_key": "fake-unsaved-key"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "fake-unsaved-key" not in response.text
    assert client.post("/api/llm/providers/missing/test").status_code == 404


def test_smart_analysis_failure_has_fixed_public_error(client, monkeypatch):
    async def fail(provider):
        raise RuntimeError("Bearer fake-secret-must-not-leak")

    monkeypatch.setattr(MockLLMProvider, "smart_analyze", fail)
    response = client.get("/api/smart-analysis")
    assert response.status_code == 502
    assert "fake-secret-must-not-leak" not in response.text
    refreshed = client.post("/api/smart-analysis/refresh")
    assert refreshed.status_code == 500
    assert "fake-secret-must-not-leak" not in refreshed.text


@pytest.mark.parametrize("upstream_status,expected", [(200, 200), (500, 502)])
def test_model_list_probe_uses_unsaved_form_and_fixed_errors(
    client, app, monkeypatch, upstream_status, expected
):
    calls, closed = [], []

    class Response:
        status_code = upstream_status
        text = "Bearer fake-unsaved-key"

        def json(self):
            return {"data": [{"id": "fixture-model", "owned_by": "fixture"}]}

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def get(self, url, headers):
            calls.append((url, headers))
            return Response()

    monkeypatch.setattr(
        "gold_monitor.services.model_probes.httpx.AsyncClient", FakeHTTPClient
    )
    response = client.post(
        "/api/llm/providers/openai/models",
        json={"api_key": "fake-unsaved-key", "base_url": "https://example.invalid"},
    )
    assert response.status_code == expected
    assert "fake-unsaved-key" not in response.text
    assert calls == [
        (
            "https://example.invalid/v1/models",
            {"Authorization": "Bearer fake-unsaved-key"},
        )
    ]
    assert closed == [True]
    provider = app.state.runtime.llm_config.get_provider("openai")
    assert provider.api_key == ""
    if expected == 200:
        assert provider.models == ["fixture-model"]
