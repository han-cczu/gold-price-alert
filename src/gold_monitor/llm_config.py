"""LLM 配置管理模块 - 支持多个模型服务平台"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 是否启用加密存储
ENCRYPT_API_KEYS = os.getenv("GOLD_ENCRYPT_API_KEYS", "false").lower() == "true"

# 配置文件路径
CONFIG_FILE = Path("llm_config.json")


@dataclass
class ModelProvider:
    """模型服务平台配置"""
    id: str = ""  # 唯一标识
    name: str = ""  # 显示名称，如 "DeepSeek", "OpenAI"
    base_url: str = ""  # API 地址
    api_key: str = ""  # API Key
    models: list = field(default_factory=list)  # 已获取的模型列表缓存
    
    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "models": self.models
        }
    
    def to_safe_dict(self) -> dict:
        """返回脱敏的配置"""
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self._mask_key(self.api_key),
            "has_api_key": bool(self.api_key),
            "models": self.models
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
    providers: list = field(default_factory=list)  # 模型服务平台列表
    active_provider_id: str = ""  # 当前使用的平台 ID
    active_model: str = ""  # 当前使用的模型
    
    def to_dict(self) -> dict:
        return {
            "providers": [p.to_dict() if isinstance(p, ModelProvider) else p for p in self.providers],
            "active_provider_id": self.active_provider_id,
            "active_model": self.active_model
        }
    
    def to_safe_dict(self) -> dict:
        """返回脱敏的配置"""
        return {
            "providers": [
                p.to_safe_dict() if isinstance(p, ModelProvider) else ModelProvider(**p).to_safe_dict() 
                for p in self.providers
            ],
            "active_provider_id": self.active_provider_id,
            "active_model": self.active_model
        }
    
    def get_active_provider(self) -> Optional[ModelProvider]:
        """获取当前激活的平台"""
        for p in self.providers:
            provider = p if isinstance(p, ModelProvider) else ModelProvider(**p)
            if provider.id == self.active_provider_id:
                return provider
        return None


class LLMConfigManager:
    """LLM 配置管理器"""
    
    def __init__(self, config_path: Optional[Path] = None, encrypt_keys: bool = None):
        self.config_path = config_path or CONFIG_FILE
        self._config: Optional[LLMConfig] = None
        self._encrypt_keys = encrypt_keys if encrypt_keys is not None else ENCRYPT_API_KEYS
        self._secret_manager = None
        
        if self._encrypt_keys:
            try:
                from .security import get_secret_manager
                self._secret_manager = get_secret_manager()
                logger.info("LLM 配置管理器启用加密存储")
            except ImportError:
                logger.warning("无法导入安全模块，API Key 将以明文存储")
                self._encrypt_keys = False
    
    def _encrypt_api_key(self, key: str) -> str:
        """加密 API Key"""
        if not self._encrypt_keys or not self._secret_manager or not key:
            return key
        # 如果已经是加密格式，不重复加密
        if key.startswith("enc:"):
            return key
        return "enc:" + self._secret_manager.encrypt(key)
    
    def _decrypt_api_key(self, key: str) -> str:
        """解密 API Key"""
        if not self._secret_manager or not key:
            return key
        # 检查是否是加密格式
        if key.startswith("enc:"):
            return self._secret_manager.decrypt(key[4:])
        return key
    
    def _load_from_file(self) -> Optional[LLMConfig]:
        """从文件加载配置"""
        if not self.config_path.exists():
            return None
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # 转换 providers 为 ModelProvider 对象，并解密 API Key
                providers = []
                for p in data.get("providers", []):
                    # 解密 API Key
                    if "api_key" in p:
                        p["api_key"] = self._decrypt_api_key(p["api_key"])
                    providers.append(ModelProvider(**p))
                
                return LLMConfig(
                    providers=providers,
                    active_provider_id=data.get("active_provider_id", ""),
                    active_model=data.get("active_model", "")
                )
        except Exception as e:
            logger.warning(f"加载 LLM 配置失败: {e}")
            return None
    
    def _create_default_config(self) -> LLMConfig:
        """创建默认配置（包含预设平台）"""
        default_providers = [
            ModelProvider(
                id="mock",
                name="Mock（模拟测试）",
                base_url="",
                api_key=""
            ),
            ModelProvider(
                id="openai",
                name="OpenAI",
                base_url="https://api.openai.com",
                api_key=""
            ),
            ModelProvider(
                id="deepseek",
                name="DeepSeek",
                base_url="https://api.deepseek.com",
                api_key=""
            ),
        ]
        return LLMConfig(
            providers=default_providers,
            active_provider_id="mock",
            active_model=""
        )
    
    def get_config(self, force_reload: bool = False) -> LLMConfig:
        """获取当前配置"""
        if self._config is None or force_reload:
            self._config = self._load_from_file()
            if self._config is None:
                self._config = self._create_default_config()
        return self._config
    
    def reload_config(self) -> LLMConfig:
        """强制重新加载配置"""
        return self.get_config(force_reload=True)
    
    def save_config(self, config: LLMConfig) -> bool:
        """保存配置到文件"""
        try:
            # 准备保存的数据，加密 API Key
            data = {
                "active_provider_id": config.active_provider_id,
                "active_model": config.active_model,
                "providers": []
            }
            
            for p in config.providers:
                provider = p if isinstance(p, ModelProvider) else ModelProvider(**p)
                provider_dict = provider.to_dict()
                # 加密 API Key
                if provider_dict.get("api_key"):
                    provider_dict["api_key"] = self._encrypt_api_key(provider_dict["api_key"])
                data["providers"].append(provider_dict)
            
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            self._config = config
            logger.info("LLM 配置已保存")
            return True
        except Exception as e:
            logger.error(f"保存 LLM 配置失败: {e}")
            return False
    
    def add_provider(self, name: str, base_url: str, api_key: str = "") -> ModelProvider:
        """添加新的模型服务平台"""
        config = self.get_config()
        provider = ModelProvider(name=name, base_url=base_url, api_key=api_key)
        config.providers.append(provider)
        self.save_config(config)
        return provider
    
    def update_provider(self, provider_id: str, **kwargs) -> Optional[ModelProvider]:
        """更新平台配置"""
        config = self.get_config()
        for i, p in enumerate(config.providers):
            provider = p if isinstance(p, ModelProvider) else ModelProvider(**p)
            if provider.id == provider_id:
                # 更新字段
                for key, value in kwargs.items():
                    if hasattr(provider, key) and value is not None:
                        setattr(provider, key, value)
                config.providers[i] = provider
                self.save_config(config)
                return provider
        return None
    
    def delete_provider(self, provider_id: str) -> bool:
        """删除平台"""
        config = self.get_config()
        original_len = len(config.providers)
        config.providers = [
            p for p in config.providers 
            if (p.id if isinstance(p, ModelProvider) else p.get("id")) != provider_id
        ]
        if len(config.providers) < original_len:
            # 如果删除的是当前激活的平台，切换到第一个
            if config.active_provider_id == provider_id and config.providers:
                first = config.providers[0]
                config.active_provider_id = first.id if isinstance(first, ModelProvider) else first.get("id")
            self.save_config(config)
            return True
        return False
    
    def set_active(self, provider_id: str, model: str = "") -> bool:
        """设置当前使用的平台和模型"""
        config = self.get_config()
        # 验证 provider 存在
        found = False
        for p in config.providers:
            pid = p.id if isinstance(p, ModelProvider) else p.get("id")
            if pid == provider_id:
                found = True
                break
        
        if not found:
            return False
        
        config.active_provider_id = provider_id
        if model:
            config.active_model = model
        self.save_config(config)
        return True
    
    def get_provider(self, provider_id: str) -> Optional[ModelProvider]:
        """获取指定平台"""
        config = self.get_config()
        for p in config.providers:
            provider = p if isinstance(p, ModelProvider) else ModelProvider(**p)
            if provider.id == provider_id:
                return provider
        return None
    
    def update_provider_models(self, provider_id: str, models: list) -> bool:
        """更新平台的模型列表缓存"""
        return self.update_provider(provider_id, models=models) is not None
    
    def reset_config(self) -> bool:
        """重置配置"""
        try:
            if self.config_path.exists():
                self.config_path.unlink()
            self._config = None
            logger.info("LLM 配置已重置")
            return True
        except Exception as e:
            logger.error(f"重置 LLM 配置失败: {e}")
            return False


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
