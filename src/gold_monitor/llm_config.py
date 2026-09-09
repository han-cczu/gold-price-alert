"""LLM 配置管理模块 - 支持多个模型服务平台"""

import json
import hashlib
import tempfile
from copy import deepcopy
from threading import RLock
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Settings

logger = logging.getLogger(__name__)

# 是否启用加密存储
ENCRYPT_API_KEYS = os.getenv("GOLD_ENCRYPT_API_KEYS", "false").lower() == "true"

# 配置文件路径
CONFIG_FILE = Path(os.getenv("GOLD_LLM_CONFIG_PATH", "llm_config.json"))


@dataclass
class ModelProvider:
    """模型服务平台配置"""

    id: str = ""  # 唯一标识
    name: str = ""  # 显示名称，如 "DeepSeek", "OpenAI"
    base_url: str = ""  # API 地址
    api_key: str = ""  # API Key
    models: list[str] = field(default_factory=list)  # 已获取的模型列表缓存

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "models": self.models,
        }

    def to_safe_dict(self) -> dict[str, Any]:
        """返回脱敏的配置"""
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self._mask_key(self.api_key),
            "has_api_key": bool(self.api_key),
            "models": self.models,
        }

    @staticmethod
    def _mask_key(key: str) -> str:
        """隐藏 API Key 中间部分"""
        if not key or len(key) < 8:
            return "****" if key else ""
        return f"{key[:4]}...{key[-4:]}"


@dataclass
class LLMConfig:
    """LLM 总配置"""

    providers: list[ModelProvider] = field(default_factory=list)  # 模型服务平台列表
    active_provider_id: str = ""  # 当前使用的平台 ID
    active_model: str = ""  # 当前使用的模型

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": [p.to_dict() for p in self.providers],
            "active_provider_id": self.active_provider_id,
            "active_model": self.active_model,
        }

    def to_safe_dict(self) -> dict[str, Any]:
        """返回脱敏的配置"""
        return {
            "providers": [p.to_safe_dict() for p in self.providers],
            "active_provider_id": self.active_provider_id,
            "active_model": self.active_model,
        }

    def get_active_provider(self) -> Optional[ModelProvider]:
        """获取当前激活的平台"""
        for provider in self.providers:
            if provider.id == self.active_provider_id:
                return provider
        return None


class LLMConfigPersistenceError(RuntimeError):
    """A configuration change could not be persisted; published state is unchanged."""


class LLMConfigLoadError(RuntimeError):
    """An existing configuration cannot be read safely."""


class LLMConfigManager:
    """Publish configuration snapshots only after an atomic file replacement."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        encrypt_keys: bool | None = None,
        *,
        settings: Settings | None = None,
        secret_manager: Any = None,
    ):
        self.settings = settings or Settings()
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else Path(getattr(self.settings, "llm_config_path", CONFIG_FILE))
        )
        self._encrypt_keys = (
            encrypt_keys
            if encrypt_keys is not None
            else getattr(self.settings, "encrypt_api_keys", ENCRYPT_API_KEYS)
        )
        self._config: Optional[LLMConfig] = None
        self._revision = 0
        self._lock = RLock()
        self._secret_manager = secret_manager
        if self._secret_manager is None and (
            self._encrypt_keys or self.settings.secret_key
        ):
            from .security import SecretManager

            if self._encrypt_keys and not self.settings.secret_key:
                raise ValueError(
                    "加密 LLM 配置需要稳定的 GOLD_SECRET_KEY 或显式密钥管理器"
                )
            self._secret_manager = SecretManager(self.settings.secret_key)

    def _encrypt_api_key(self, key: str) -> str:
        if not self._encrypt_keys or not key:
            return key
        if self._secret_manager is None:
            raise ValueError("缺少 API Key 加密器")
        encrypted = self._secret_manager.encrypt(key)
        if not encrypted:
            raise ValueError("API Key 加密失败")
        return "enc:" + encrypted

    def _decrypt_api_key(self, key: str) -> str:
        if not key.startswith("enc:"):
            return key
        if self._secret_manager is None:
            raise ValueError("读取已加密 API Key 需要配置 GOLD_SECRET_KEY")
        decrypted = self._secret_manager.decrypt(key[4:])
        if not decrypted:
            raise ValueError("API Key 解密失败")
        return decrypted

    def _load_from_file(self) -> Optional[LLMConfig]:
        try:
            with self.config_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            providers = []
            for item in data.get("providers", []):
                item = dict(item)
                item["api_key"] = self._decrypt_api_key(item.get("api_key", ""))
                providers.append(ModelProvider(**item))
            return LLMConfig(
                providers=providers,
                active_provider_id=data.get("active_provider_id", ""),
                active_model=data.get("active_model", ""),
            )
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise LLMConfigLoadError("无法读取 LLM 配置，原有效配置未变更") from exc

    def _create_default_config(self) -> LLMConfig:
        """Use application environment defaults only when no saved file exists."""
        default_providers = [
            ModelProvider(id="mock", name="Mock（模拟测试）", base_url="", api_key=""),
            ModelProvider(
                id="openai",
                name="OpenAI",
                base_url=self.settings.openai_base_url or "https://api.openai.com",
                api_key=self.settings.openai_api_key,
            ),
            ModelProvider(
                id="deepseek",
                name="DeepSeek",
                base_url="https://api.deepseek.com",
                api_key="",
            ),
            ModelProvider(
                id="anthropic",
                name="Anthropic Claude",
                base_url="https://api.anthropic.com",
                api_key=self.settings.anthropic_api_key,
            ),
        ]
        requested = self.settings.llm_provider.strip().lower()
        active_id = next(
            (
                provider.id
                for provider in default_providers
                if provider.id == requested and provider.api_key
            ),
            "mock",
        )
        return LLMConfig(
            providers=default_providers, active_provider_id=active_id, active_model=""
        )

    @staticmethod
    def config_fingerprint(config: LLMConfig) -> str:
        serialized = json.dumps(config.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self.config_fingerprint(self.get_config())

    @property
    def revision(self) -> int:
        return self._revision

    def get_config(self, force_reload: bool = False) -> LLMConfig:
        """Return a detached snapshot so callers cannot mutate published state."""
        with self._lock:
            if self._config is None or force_reload:
                candidate = self._load_from_file() or self._create_default_config()
                if self._config is None or self.config_fingerprint(
                    candidate
                ) != self.config_fingerprint(self._config):
                    self._config = candidate
                    self._revision += 1
            return deepcopy(self._config)

    def reload_config(self) -> LLMConfig:
        return self.get_config(force_reload=True)

    def save_config(self, config: LLMConfig) -> bool:
        """Write beside the target, fsync, then atomically replace and publish."""
        with self._lock:
            temporary: Path | None = None
            try:
                candidate = deepcopy(config)
                data = candidate.to_dict()
                for provider in data["providers"]:
                    provider["api_key"] = self._encrypt_api_key(provider["api_key"])
                # Serialize before creating a temporary file or touching the target.
                serialized = json.dumps(data, indent=2, ensure_ascii=False)
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.config_path.parent,
                    prefix=f".{self.config_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.config_path)
                self._config = candidate
                self._revision += 1
                return True
            except Exception as exc:
                raise LLMConfigPersistenceError(
                    "保存 LLM 配置失败，原有效配置未变更"
                ) from exc
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("无法清理未发布的 LLM 配置临时文件")

    def add_provider(
        self, name: str, base_url: str, api_key: str = ""
    ) -> ModelProvider:
        """添加新的模型服务平台"""
        with self._lock:
            config = self.get_config()
            provider = ModelProvider(name=name, base_url=base_url, api_key=api_key)
            config.providers.append(provider)
            self.save_config(config)
            return provider

    def update_provider(self, provider_id: str, **kwargs) -> Optional[ModelProvider]:
        """更新平台配置"""
        with self._lock:
            config = self.get_config()
            for i, provider in enumerate(config.providers):
                if provider.id == provider_id:
                    # 更新字段
                    for key, value in kwargs.items():
                        if (
                            key in {"name", "base_url", "api_key", "models"}
                            and value is not None
                        ):
                            if key == "api_key" and (
                                not value or "..." in value or value == "****"
                            ):
                                continue
                            setattr(provider, key, value)
                    config.providers[i] = provider
                    self.save_config(config)
                    return provider
            return None

    def delete_provider(self, provider_id: str) -> bool:
        """删除平台"""
        with self._lock:
            config = self.get_config()
            original_len = len(config.providers)
            config.providers = [p for p in config.providers if p.id != provider_id]
            if len(config.providers) < original_len:
                # 如果删除的是当前激活的平台，切换到第一个
                if config.active_provider_id == provider_id and config.providers:
                    first = config.providers[0]
                    config.active_provider_id = first.id
                    config.active_model = ""
                self.save_config(config)
                return True
            return False

    def set_active(self, provider_id: str, model: str = "") -> bool:
        """设置当前使用的平台和模型"""
        with self._lock:
            config = self.get_config()
            # 验证 provider 存在
            found = False
            for provider in config.providers:
                if provider.id == provider_id:
                    found = True
                    break

            if not found:
                return False

            if model or config.active_provider_id != provider_id:
                config.active_model = model
            config.active_provider_id = provider_id
            self.save_config(config)
            return True

    def get_provider(self, provider_id: str) -> Optional[ModelProvider]:
        """获取指定平台"""
        config = self.get_config()
        for provider in config.providers:
            if provider.id == provider_id:
                return provider
        return None

    def update_provider_models(self, provider_id: str, models: list) -> bool:
        """更新平台的模型列表缓存"""
        return self.update_provider(provider_id, models=models) is not None

    def reset_config(self) -> bool:
        """Reset through the same atomic commit path as other changes."""
        return self.save_config(self._create_default_config())


# 全局配置管理器实例
_config_manager: Optional[LLMConfigManager] = None


def get_llm_config_manager() -> LLMConfigManager:
    """获取全局配置管理器"""
    global _config_manager
    if _config_manager is None:
        _config_manager = LLMConfigManager()
    return _config_manager


def get_llm_config() -> LLMConfig:
    """获取当前 LLM 配置"""
    return get_llm_config_manager().get_config()
