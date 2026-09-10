"""Model probe caches are tied to the saved endpoint and credentials."""

import pytest

from gold_monitor.analysis.factory import provider_from_config
from gold_monitor.llm_config import LLMConfigManager
from gold_monitor.services.model_probes import ModelProbeService


@pytest.fixture
def manager(app_settings):
    manager = LLMConfigManager(settings=app_settings)
    manager.update_provider(
        "openai",
        base_url="https://saved.invalid",
        api_key="fixture-saved-key",
        models=["saved-model"],
    )
    manager.set_active("openai")
    return manager


def fake_models(monkeypatch, during_request=lambda: None):
    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "probed-model"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers):
            during_request()
            return Response()

    monkeypatch.setattr("gold_monitor.services.model_probes.httpx.AsyncClient", Client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://unsaved.invalid"},
        {"api_key": "fixture-other-account"},
        {"base_url": "https://unsaved.invalid", "api_key": "fixture-other-account"},
    ],
)
async def test_unsaved_probe_does_not_change_saved_model(
    manager, monkeypatch, overrides
):
    fake_models(monkeypatch)
    original = manager.config_path.read_bytes()
    response = await ModelProbeService(manager, manager.settings).fetch_models(
        "openai", **overrides
    )
    assert response["models"][0]["id"] == "probed-model"
    assert manager.config_path.read_bytes() == original
    assert (
        provider_from_config(manager.reload_config(), None, manager.settings).model
        == "saved-model"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_key", [None, "", "****", "fixt...-key", "fixture-saved-key"]
)
async def test_saved_or_masked_credentials_update_model_cache(
    manager, monkeypatch, api_key
):
    fake_models(monkeypatch)
    await ModelProbeService(manager, manager.settings).fetch_models(
        "openai", api_key=api_key
    )
    assert manager.reload_config().get_active_provider().models == ["probed-model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["url", "key", "delete", "external"])
async def test_probe_cannot_overwrite_config_changed_during_request(
    manager, monkeypatch, change
):
    def change_config():
        if change == "url":
            manager.update_provider("openai", base_url="https://changed.invalid")
        elif change == "key":
            manager.update_provider("openai", api_key="fixture-rotated-key")
        elif change == "delete":
            manager.delete_provider("openai")
        else:
            external = LLMConfigManager(settings=manager.settings)
            external.update_provider("openai", base_url="https://external.invalid")

    fake_models(monkeypatch, change_config)
    response = await ModelProbeService(manager, manager.settings).fetch_models("openai")
    assert response["models"][0]["id"] == "probed-model"
    current = manager.reload_config()
    provider = next((item for item in current.providers if item.id == "openai"), None)
    if change == "delete":
        assert provider is None
    else:
        assert provider.models == ["saved-model"]
