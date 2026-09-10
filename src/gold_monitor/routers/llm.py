"""LLM configuration routes use resources owned by the current application."""

import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from ..dependencies import get_runtime, require_admin_dep
from ..schemas import (
    ProviderProbeRequest,
    ProviderRequest,
    ProviderUpdateRequest,
    SetActiveRequest,
    TestConnectionRequest,
)
from ..services.model_probes import ModelProbeService, ModelProbeError

router = APIRouter(dependencies=[Depends(require_admin_dep)])


@router.get("/api/llm/config")
async def get_llm_config(runtime=Depends(get_runtime)):
    config = await asyncio.to_thread(runtime.llm_config.reload_config)
    return config.to_safe_dict()


@router.get("/api/llm/status")
async def get_llm_status(runtime=Depends(get_runtime)):
    """获取当前 AI 分析使用的状态"""
    config = await asyncio.to_thread(runtime.llm_config.reload_config)
    active_provider = config.get_active_provider()

    if not active_provider:
        return {
            "enabled": False,
            "mode": "mock",
            "message": "未配置，使用模拟分析",
            "provider_name": None,
            "model": None,
        }

    if active_provider.id == "mock":
        return {
            "enabled": False,
            "mode": "mock",
            "message": "Mock 模式，返回固定分析结果",
            "provider_name": "Mock",
            "model": None,
        }

    if not active_provider.api_key:
        return {
            "enabled": False,
            "mode": "mock",
            "message": f"平台 {active_provider.name} 未配置 API Key，使用模拟分析",
            "provider_name": active_provider.name,
            "model": None,
        }

    return {
        "enabled": True,
        "mode": "ai",
        "message": "已启用 AI 分析",
        "provider_name": active_provider.name,
        "model": config.active_model or "(默认模型)",
    }


@router.get("/api/llm/providers")
async def get_providers(runtime=Depends(get_runtime)):
    config = await asyncio.to_thread(runtime.llm_config.get_config)
    return config.to_safe_dict()


@router.post("/api/llm/providers")
async def add_provider(request: ProviderRequest, runtime=Depends(get_runtime)):
    provider = await asyncio.to_thread(
        runtime.llm_config.add_provider,
        name=request.name,
        base_url=request.base_url,
        api_key=request.api_key or "",
    )
    return {"success": True, "provider": provider.to_safe_dict()}


@router.put("/api/llm/providers/{provider_id}")
async def update_provider(
    provider_id: str, request: ProviderUpdateRequest, runtime=Depends(get_runtime)
):
    provider = await asyncio.to_thread(
        runtime.llm_config.update_provider,
        provider_id,
        name=request.name,
        base_url=request.base_url,
        api_key=request.api_key,
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "provider": provider.to_safe_dict()}


@router.delete("/api/llm/providers/{provider_id}")
async def delete_provider(provider_id: str, runtime=Depends(get_runtime)):
    if provider_id == "mock":
        raise HTTPException(status_code=400, detail="不能删除默认的 Mock 平台")
    if not await asyncio.to_thread(runtime.llm_config.delete_provider, provider_id):
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "平台已删除"}


@router.post("/api/llm/active")
async def set_active_provider(request: SetActiveRequest, runtime=Depends(get_runtime)):
    if not await asyncio.to_thread(
        runtime.llm_config.set_active, request.provider_id, request.model or ""
    ):
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "已切换"}


@router.post("/api/llm/providers/{provider_id}/models")
async def fetch_provider_models(
    provider_id: str,
    request: Optional[ProviderProbeRequest] = None,
    runtime=Depends(get_runtime),
):
    service = ModelProbeService(runtime.llm_config, runtime.config)
    try:
        return await service.fetch_models(
            provider_id,
            api_key=request.api_key if request else None,
            base_url=request.base_url if request else None,
        )
    except ModelProbeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/api/llm/providers/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    request: Optional[TestConnectionRequest] = None,
    runtime=Depends(get_runtime),
):
    service = ModelProbeService(runtime.llm_config, runtime.config)
    try:
        return await service.test_connection(
            provider_id,
            api_key=request.api_key if request else None,
            base_url=request.base_url if request else None,
            model=request.model if request else None,
        )
    except ModelProbeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/api/llm/config")
async def reset_llm_config(runtime=Depends(get_runtime)):
    await asyncio.to_thread(runtime.llm_config.reset_config)
    return {"message": "配置已重置", "success": True}
