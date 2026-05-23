"""API routers - 按领域拆分的路由模块

每个子模块导出一个 `router = APIRouter()`，由 web.py 统一 include。
路由路径、参数、响应、状态码均与拆分前的 web.py 保持一致。
"""

from . import (
    price,
    alerts,
    analysis,
    llm,
    data,
    notifications,
    collector,
    market,
    source_quality,
    system,
)

__all__ = [
    "price",
    "alerts",
    "analysis",
    "llm",
    "data",
    "notifications",
    "collector",
    "market",
    "source_quality",
    "system",
]
