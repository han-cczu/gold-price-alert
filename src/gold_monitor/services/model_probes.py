"""Inspect saved or unsaved model settings without exposing supplier errors."""

import asyncio
from dataclasses import replace
from typing import Any

import httpx

from ..analysis.factory import provider_from_config, resolve_model
from ..analysis.providers import AnthropicProvider, OpenAIProvider
from ..config import Settings
from ..llm_config import LLMConfig, LLMConfigManager, ModelProvider


class ModelProbeError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def resolve_credentials(
    provider: ModelProvider, api_key: str | None, base_url: str | None
) -> tuple[str, str]:
    key = (api_key or "").strip()
    if not key or "..." in key or key == "****":
        key = provider.api_key
    return key, (base_url or "").strip() or provider.base_url


class ModelProbeService:
    def __init__(self, manager: LLMConfigManager, settings: Settings):
        self.manager, self.settings = manager, settings

    async def _resolve(
        self,
        provider_id: str,
        api_key: str | None,
        base_url: str | None,
        model: str | None = None,
    ) -> tuple[ModelProvider, LLMConfig]:
        saved = await asyncio.to_thread(self.manager.reload_config)
        provider = next(
            (item for item in saved.providers if item.id == provider_id), None
        )
        if provider is None:
            raise ModelProbeError(404, "平台不存在")
        key, url = resolve_credentials(provider, api_key, base_url)
        effective = replace(provider, api_key=key, base_url=url)
        selected_model = resolve_model(
            effective,
            model
            or (
                saved.active_model if saved.active_provider_id == provider_id else None
            ),
        )
        return effective, LLMConfig([effective], provider_id, selected_model or "")

    async def fetch_models(
        self,
        provider_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        effective, config = await self._resolve(provider_id, api_key, base_url)
        if provider_id == "mock":
            return {
                "success": True,
                "models": [],
                "count": 0,
                "message": "Mock 模式无模型",
            }
        if not effective.api_key:
            raise ModelProbeError(400, "请先填写 API Key")
        if not effective.base_url:
            raise ModelProbeError(400, "请先填写 API 地址")
        provider = provider_from_config(config, None, self.settings)
        try:
            headers = {"Authorization": f"Bearer {effective.api_key}"}
            if isinstance(provider, AnthropicProvider):
                endpoint = effective.base_url.rstrip("/")
                endpoint = endpoint if endpoint.endswith("/v1") else endpoint + "/v1"
                headers = {
                    "x-api-key": effective.api_key,
                    "anthropic-version": "2023-06-01",
                }
            else:
                endpoint = OpenAIProvider._normalize_base_url(effective.base_url) or ""
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{endpoint}/models", headers=headers)
                if response.status_code == 401:
                    raise ModelProbeError(401, "API Key 无效")
                if response.status_code != 200:
                    raise ModelProbeError(502, "模型服务拒绝了模型列表请求")
                data = response.json()
            raw_models = data.get("data", [])
            if not isinstance(raw_models, list):
                raise ValueError("invalid model list")
            models = [
                {
                    "id": item["id"],
                    "owned_by": item.get("owned_by", ""),
                    "created": item.get("created", 0),
                }
                for item in raw_models
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"]
            ]
            models.sort(key=self._model_order)
        except httpx.TimeoutException as exc:
            raise ModelProbeError(504, "请求超时") from exc
        except httpx.RequestError as exc:
            raise ModelProbeError(502, "无法连接模型服务") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModelProbeError(502, "模型服务返回了无效数据") from exc
        finally:
            await provider.close()
        # Persist only the model cache. Unsaved form credentials stay unsaved.
        await asyncio.to_thread(
            self.manager.update_provider_models,
            provider_id,
            [item["id"] for item in models],
        )
        return {"success": True, "models": models, "count": len(models)}

    @staticmethod
    def _model_order(model: dict) -> tuple[int, str]:
        identifier = model["id"].lower()
        for priority, keyword in enumerate(
            ("gpt-4", "gpt-3.5", "chat", "turbo"), start=1
        ):
            if keyword in identifier:
                return priority, model["id"]
        return 10, model["id"]

    async def test_connection(
        self,
        provider_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        effective, config = await self._resolve(provider_id, api_key, base_url, model)
        result: dict[str, Any] = {
            "provider_id": provider_id,
            "provider_name": effective.name,
            "model": config.active_model or None,
            "success": False,
            "message": "",
            "response": None,
        }
        if provider_id == "mock":
            result.update(success=True, message="Mock 模式无需连接测试")
            return result
        if not effective.api_key or not effective.base_url:
            result["message"] = (
                "未配置 API Key" if not effective.api_key else "未配置 API 地址"
            )
            return result
        provider = provider_from_config(config, None, self.settings)
        result["model"] = getattr(provider, "model", config.active_model)
        try:
            reply = await provider.probe()
            # A proxy may echo request secrets even on a successful response.
            reply = reply.replace(effective.api_key, "[REDACTED]")
            result.update(
                success=True,
                message=f"连接成功 (模型: {result['model']})",
                response=reply[:100] if reply else "OK",
            )
        except Exception:
            result["message"] = "连接失败，请检查模型、地址与凭据"
        finally:
            await provider.close()
        return result
