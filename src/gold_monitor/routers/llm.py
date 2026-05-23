"""LLM 配置相关路由：/api/llm/*"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..llm_config import get_llm_config_manager, ModelProvider
from ..schemas import (
    ProviderRequest, ProviderUpdateRequest, SetActiveRequest, TestConnectionRequest
)
from ..state import require_admin_dep

router = APIRouter()


@router.get("/api/llm/config")
async def get_llm_config():
    """获取 LLM 配置（API Key 脱敏）"""
    manager = get_llm_config_manager()
    config = manager.reload_config()  # 强制重新加载
    return config.to_safe_dict()


@router.get("/api/llm/status")
async def get_llm_status():
    """获取当前 AI 分析使用的状态"""
    manager = get_llm_config_manager()
    config = manager.reload_config()
    active_provider = config.get_active_provider()

    if not active_provider:
        return {
            "enabled": False,
            "mode": "mock",
            "message": "未配置，使用模拟分析",
            "provider_name": None,
            "model": None
        }

    if active_provider.id == "mock":
        return {
            "enabled": False,
            "mode": "mock",
            "message": "Mock 模式，返回固定分析结果",
            "provider_name": "Mock",
            "model": None
        }

    if not active_provider.api_key:
        return {
            "enabled": False,
            "mode": "mock",
            "message": f"平台 {active_provider.name} 未配置 API Key，使用模拟分析",
            "provider_name": active_provider.name,
            "model": None
        }

    return {
        "enabled": True,
        "mode": "ai",
        "message": "已启用 AI 分析",
        "provider_name": active_provider.name,
        "model": config.active_model or "(默认模型)"
    }


@router.get("/api/llm/providers")
async def get_providers():
    """获取所有模型服务平台"""
    manager = get_llm_config_manager()
    config = manager.get_config()
    return {
        "providers": [
            p.to_safe_dict() if isinstance(p, ModelProvider) else ModelProvider(**p).to_safe_dict()
            for p in config.providers
        ],
        "active_provider_id": config.active_provider_id,
        "active_model": config.active_model
    }


@router.post("/api/llm/providers")
async def add_provider(request: ProviderRequest):
    """添加新的模型服务平台"""
    manager = get_llm_config_manager()
    provider = manager.add_provider(
        name=request.name,
        base_url=request.base_url,
        api_key=request.api_key or ""
    )
    return {"success": True, "provider": provider.to_safe_dict()}


@router.put("/api/llm/providers/{provider_id}")
async def update_provider(provider_id: str, request: ProviderUpdateRequest):
    """更新平台配置"""
    manager = get_llm_config_manager()

    # 获取当前平台配置
    current = manager.get_provider(provider_id)
    if not current:
        raise HTTPException(status_code=404, detail="平台不存在")

    # 如果 api_key 是脱敏格式或为空，保留原有的
    new_api_key = request.api_key
    if not new_api_key or "..." in new_api_key:
        new_api_key = current.api_key

    provider = manager.update_provider(
        provider_id,
        name=request.name,
        base_url=request.base_url,
        api_key=new_api_key
    )

    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")

    return {"success": True, "provider": provider.to_safe_dict()}


@router.delete("/api/llm/providers/{provider_id}")
async def delete_provider(provider_id: str):
    """删除平台"""
    # 不允许删除 mock
    if provider_id == "mock":
        raise HTTPException(status_code=400, detail="不能删除默认的 Mock 平台")

    manager = get_llm_config_manager()
    success = manager.delete_provider(provider_id)
    if not success:
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "平台已删除"}


@router.post("/api/llm/active")
async def set_active_provider(request: SetActiveRequest):
    """设置当前使用的平台和模型"""
    manager = get_llm_config_manager()
    success = manager.set_active(request.provider_id, request.model or "")
    if not success:
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "已切换"}


@router.post("/api/llm/providers/{provider_id}/models")
async def fetch_provider_models(provider_id: str):
    """获取指定平台的模型列表"""
    import httpx

    manager = get_llm_config_manager()
    provider = manager.get_provider(provider_id)

    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")

    if provider_id == "mock":
        return {"success": True, "models": [], "count": 0, "message": "Mock 模式无模型"}

    if not provider.api_key:
        raise HTTPException(status_code=400, detail="请先配置 API Key")

    if not provider.base_url:
        raise HTTPException(status_code=400, detail="请先配置 API 地址")

    # 智能拼接 URL
    url = provider.base_url.rstrip('/')
    if not url.endswith('/v1') and '/v1' not in url:
        models_url = f"{url}/v1/models"
    else:
        models_url = f"{url}/models"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {provider.api_key}"}
            )

            if resp.status_code == 401:
                raise HTTPException(status_code=401, detail="API Key 无效")

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"获取模型失败: {resp.text[:200]}"
                )

            data = resp.json()
            models = data.get("data", [])

            # 提取模型 ID 并排序
            model_list = []
            for m in models:
                model_id = m.get("id", "")
                if model_id:
                    model_list.append({
                        "id": model_id,
                        "owned_by": m.get("owned_by", ""),
                        "created": m.get("created", 0)
                    })

            # 排序
            def sort_key(m):
                id_lower = m["id"].lower()
                priority = 10
                if "gpt-4" in id_lower:
                    priority = 1
                elif "gpt-3.5" in id_lower:
                    priority = 2
                elif "chat" in id_lower:
                    priority = 3
                elif "turbo" in id_lower:
                    priority = 4
                return (priority, m["id"])

            model_list.sort(key=sort_key)

            # 缓存模型列表
            manager.update_provider_models(provider_id, [m["id"] for m in model_list])

            return {
                "success": True,
                "models": model_list,
                "count": len(model_list)
            }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="请求超时")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"网络错误: {str(e)}")


@router.post("/api/llm/providers/{provider_id}/test")
async def test_provider_connection(provider_id: str, request: Optional[TestConnectionRequest] = None):
    """测试指定平台的连接 - 简单发送 hi 测试"""
    manager = get_llm_config_manager()
    provider = manager.get_provider(provider_id)

    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")

    # 获取要测试的模型
    test_model = request.model if request and request.model else None

    # 如果没有指定模型，尝试从缓存的模型列表或根据名称推断
    if not test_model:
        if provider.models:
            test_model = provider.models[0]
        else:
            provider_name = (provider.name or "").lower()
            if "deepseek" in provider_name:
                test_model = "deepseek-chat"
            elif "qwen" in provider_name or "通义" in provider_name:
                test_model = "qwen-turbo"
            elif "moonshot" in provider_name or "kimi" in provider_name:
                test_model = "moonshot-v1-8k"
            elif "zhipu" in provider_name or "glm" in provider_name:
                test_model = "glm-4"
            elif "openai" in provider_name:
                test_model = "gpt-4o-mini"
            # 如果都不匹配，保持 None 让下面的代码处理

    result = {
        "provider_id": provider_id,
        "provider_name": provider.name,
        "model": test_model,
        "success": False,
        "message": "",
        "response": None
    }

    if provider_id == "mock":
        result["success"] = True
        result["message"] = "Mock 模式无需连接测试"
        return result

    if not provider.api_key:
        result["message"] = "未配置 API Key"
        return result

    if not provider.base_url:
        result["message"] = "未配置 API 地址"
        return result

    if not test_model:
        result["message"] = "请先获取模型列表或手动指定测试模型"
        return result

    try:
        from openai import AsyncOpenAI
        from ..analyzer import OpenAIProvider

        # 标准化 URL
        base_url = OpenAIProvider._normalize_base_url(provider.base_url)

        # 创建客户端
        client = AsyncOpenAI(api_key=provider.api_key, base_url=base_url)

        # 简单发送 "hi" 测试连接
        response = await client.chat.completions.create(
            model=test_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50
        )

        reply = response.choices[0].message.content

        result["success"] = True
        result["message"] = f"连接成功 (模型: {test_model})"
        result["response"] = reply[:100] if reply else "OK"

    except Exception as e:
        result["message"] = f"连接失败: {str(e)}"

    return result


@router.delete("/api/llm/config")
async def reset_llm_config(_admin: bool = Depends(require_admin_dep)):
    """重置 LLM 配置（需要管理员权限）"""
    manager = get_llm_config_manager()
    success = manager.reset_config()

    if not success:
        raise HTTPException(status_code=500, detail="重置配置失败")

    return {"message": "配置已重置", "success": True}
