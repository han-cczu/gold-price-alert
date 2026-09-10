"""Configuration publication, rollback, and compatibility without real keys."""

import json

import pytest

from gold_monitor.config import Settings
from gold_monitor.llm_config import (
    LLMConfigLoadError,
    LLMConfigManager,
    LLMConfigPersistenceError,
)
from gold_monitor.analysis.factory import create_llm_provider
from gold_monitor.analysis.providers import (
    AnthropicProvider,
    MockLLMProvider,
    OpenAIProvider,
)


@pytest.fixture
def manager(tmp_path):
    manager = LLMConfigManager(tmp_path / "llm.json", encrypt_keys=False)
    manager.save_config(manager.get_config())
    return manager


@pytest.mark.parametrize(
    "operation", ["add", "update", "delete", "active", "models", "reset"]
)
def test_mutations_keep_memory_and_file_when_replace_fails(
    manager, monkeypatch, operation
):
    original_bytes = manager.config_path.read_bytes()
    original_config = manager.get_config().to_dict()
    original_revision = manager.revision

    def fail_replace(source, destination):
        raise PermissionError("simulated file lock")

    monkeypatch.setattr("gold_monitor.llm_config.os.replace", fail_replace)
    operations = {
        "add": lambda: manager.add_provider("test", "https://example.invalid"),
        "update": lambda: manager.update_provider("openai", name="changed"),
        "delete": lambda: manager.delete_provider("openai"),
        "active": lambda: manager.set_active("openai", "model-v1"),
        "models": lambda: manager.update_provider_models("openai", ["model-v1"]),
        "reset": manager.reset_config,
    }
    with pytest.raises(LLMConfigPersistenceError, match="原有效配置未变更"):
        operations[operation]()
    assert manager.config_path.read_bytes() == original_bytes
    assert manager.get_config().to_dict() == original_config
    assert manager.revision == original_revision
    assert list(manager.config_path.parent.glob("*.tmp")) == []


def test_fsync_failure_does_not_truncate_existing_file(manager, monkeypatch):
    before = manager.config_path.read_bytes()
    monkeypatch.setattr(
        "gold_monitor.llm_config.os.fsync",
        lambda _: (_ for _ in ()).throw(OSError("disk error")),
    )
    with pytest.raises(LLMConfigPersistenceError):
        manager.add_provider("new", "")
    assert manager.config_path.read_bytes() == before
    assert all(item.name != "new" for item in manager.get_config().providers)


def test_configuration_snapshots_and_masked_keys(manager):
    manager.update_provider("openai", api_key="test-fake-api-key")
    original = manager.get_config()
    original.providers[1].api_key = "mutated outside manager"
    assert manager.get_provider("openai").api_key == "test-fake-api-key"
    for value in ("", "****", "test...-key"):
        manager.update_provider("openai", api_key=value)
        assert manager.get_provider("openai").api_key == "test-fake-api-key"


def test_switch_provider_resets_foreign_model(manager):
    manager.set_active("openai", "supplier-a-model")
    manager.set_active("deepseek")
    assert manager.get_config().active_model == ""


def test_malformed_existing_file_reports_error_without_publishing_defaults(manager):
    before = manager.get_config().to_dict()
    manager.config_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(LLMConfigLoadError):
        manager.reload_config()
    assert manager.get_config().to_dict() == before


def test_encrypted_config_roundtrip_with_injected_cipher(tmp_path):
    class Cipher:
        def encrypt(self, value):
            return value[::-1]

        def decrypt(self, value):
            return value[::-1]

    path = tmp_path / "private.json"
    manager = LLMConfigManager(path, encrypt_keys=True, secret_manager=Cipher())
    manager.update_provider("openai", api_key="test-fake-api-key")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"][1]["api_key"].startswith("enc:")
    assert "test-fake-api-key" not in path.read_text(encoding="utf-8")
    reloaded = LLMConfigManager(path, encrypt_keys=True, secret_manager=Cipher())
    assert reloaded.get_provider("openai").api_key == "test-fake-api-key"


def test_settings_are_explicit_and_isolated(tmp_path):
    configured = Settings(
        _env_file=None,
        llm_config_path=str(tmp_path / "app.json"),
        encrypt_api_keys=False,
    )
    manager = LLMConfigManager(settings=configured)
    manager.set_active("mock")
    assert manager.config_path == tmp_path / "app.json"
    assert manager.config_path.exists()


@pytest.mark.parametrize(
    "selected,key,expected",
    [
        ("openai", "fake-environment-key", OpenAIProvider),
        ("anthropic", "fake-environment-key", AnthropicProvider),
        ("openai", "", MockLLMProvider),
        ("anthropic", "", MockLLMProvider),
        ("mock", "fake-environment-key", MockLLMProvider),
    ],
)
def test_missing_file_uses_injected_environment_defaults(
    tmp_path, selected, key, expected
):
    settings = Settings(
        _env_file=None,
        llm_provider=selected,
        openai_api_key=key,
        anthropic_api_key=key,
        openai_base_url="https://example.invalid/compatible",
        llm_config_path=str(tmp_path / "missing.json"),
        encrypt_api_keys=False,
    )
    provider = create_llm_provider(settings=settings)
    assert isinstance(provider, expected)
    if isinstance(provider, OpenAIProvider):
        assert provider.api_key == key
        assert provider.base_url == "https://example.invalid/compatible/v1"
    assert not (tmp_path / "missing.json").exists()


def test_saved_mock_selection_wins_over_environment_and_legacy_entry(
    tmp_path, monkeypatch
):
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="fake-env-key",
        anthropic_api_key="",
        llm_config_path=str(tmp_path / "saved.json"),
        encrypt_api_keys=False,
    )
    manager = LLMConfigManager(settings=settings)
    manager.set_active("mock")
    original = manager.config_path.read_bytes()
    monkeypatch.setenv("GOLD_LLM_PROVIDER", "openai")
    monkeypatch.setattr("gold_monitor.llm_config._config_manager", manager)
    assert isinstance(create_llm_provider(), MockLLMProvider)
    assert isinstance(create_llm_provider(settings=settings), MockLLMProvider)
    assert manager.config_path.read_bytes() == original


def test_existing_file_empty_key_is_not_filled_from_environment(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="fake-env-key",
        anthropic_api_key="",
        llm_config_path=str(tmp_path / "saved.json"),
        encrypt_api_keys=False,
    )
    manager = LLMConfigManager(settings=settings)
    config = manager.get_config()
    config.get_active_provider().api_key = ""
    manager.save_config(config)
    assert isinstance(create_llm_provider(settings=settings), MockLLMProvider)
    assert LLMConfigManager(settings=settings).get_provider("openai").api_key == ""


def test_conditional_model_cache_failure_preserves_file_and_memory(
    manager, monkeypatch
):
    manager.update_provider("openai", api_key="fixture-key", models=["existing"])
    current = manager.get_provider("openai")
    before = manager.config_path.read_bytes()

    def fail(*args):
        raise OSError("fixture write failure")

    monkeypatch.setattr("gold_monitor.llm_config.os.replace", fail)
    with pytest.raises(LLMConfigPersistenceError):
        manager.update_provider_models_if_current(
            "openai",
            ["new-model"],
            expected_base_url=current.base_url,
            expected_api_key=current.api_key,
        )
    assert manager.config_path.read_bytes() == before
    assert manager.get_provider("openai").models == ["existing"]
