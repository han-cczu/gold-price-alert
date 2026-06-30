"""共享状态与依赖

将 web.py 中跨 routers / lifespan 共用的全局状态、WebSocket 管理器、
鉴权依赖、缓存、价格更新回调与智能分析逻辑下沉到此模块，避免循环导入。

设计要点：
- 模块级可变状态（如 _alert_monitor）通过 getter/setter 访问，lifespan 写入、
  routers 读取，确保大家拿到的是同一对象。
- 不引入对 web.py 的依赖，仅依赖更底层的业务模块。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, WebSocket

from .config import settings
from .models import Database
from .alert import AlertMonitor, Alert
from .llm_config import get_llm_config_manager
from .data_sources.base import PriceData
from .security import get_api_key_auth, get_rate_limiter, APIKeyAuth, RateLimiter
from .metrics import (
    record_price,
    record_alert,
    update_ws_connections,
    record_ws_message,
)

logger = logging.getLogger(__name__)

# 全局数据库实例
db = Database(settings.database_url)
db.create_tables()

# 应用启动时间（naive UTC，与全项目时间体系一致）
_app_start_time = datetime.now(timezone.utc).replace(tzinfo=None)

# ============ 全局状态 ============

_alert_monitor: Optional[AlertMonitor] = None
_alerts_buffer: list[Alert] = []  # 最近的告警缓存

# 智能分析缓存
_smart_analysis_cache: Optional[dict] = None
_smart_analysis_lock = asyncio.Lock()

# 安全组件
_api_auth: Optional[APIKeyAuth] = None
_rate_limiter: Optional[RateLimiter] = None

# 定时任务：每天0点自动分析
_daily_analysis_task: Optional[asyncio.Task] = None

# 汇率缓存（简单内存缓存，避免频繁请求）
_exchange_rate_cache = {"usd_cny": 7.2, "updated_at": None}


def get_app_start_time() -> datetime:
    return _app_start_time


# ---- alert_monitor 访问器 ----


def get_alert_monitor() -> Optional[AlertMonitor]:
    return _alert_monitor


def set_alert_monitor(monitor: Optional[AlertMonitor]) -> None:
    global _alert_monitor
    _alert_monitor = monitor


# ---- daily analysis task 访问器 ----


def get_daily_analysis_task() -> Optional[asyncio.Task]:
    return _daily_analysis_task


def set_daily_analysis_task(task: Optional[asyncio.Task]) -> None:
    global _daily_analysis_task
    _daily_analysis_task = task


# ---- smart analysis cache 访问器 ----


def get_smart_analysis_cache() -> Optional[dict]:
    return _smart_analysis_cache


# ---- 安全组件访问器 ----


def get_auth() -> APIKeyAuth:
    """获取 API 鉴权组件"""
    global _api_auth
    if _api_auth is None:
        _api_auth = get_api_key_auth()
    return _api_auth


def get_limiter() -> RateLimiter:
    """获取限流组件"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = get_rate_limiter(settings.rate_limit_per_minute)
    return _rate_limiter


async def require_admin_dep(request: Request):
    """管理员鉴权依赖项 - 独立于安全中间件的纵深防御。

    即使 ADMIN_PATHS 前缀配置遗漏，挂了此依赖的高危端点仍受保护。
    与中间件一致：仅在 enable_auth=True 时强制校验。
    """
    return await get_auth().require_admin(request)


# ============ WebSocket 连接管理器 ============


class ConnectionManager:
    """WebSocket 连接管理器 - 管理所有客户端连接"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """接受新连接"""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info(
            "WebSocket 客户端连接，当前连接数: %d", len(self.active_connections)
        )

    async def disconnect(self, websocket: WebSocket):
        """断开连接"""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(
            "WebSocket 客户端断开，当前连接数: %d", len(self.active_connections)
        )

    async def broadcast(self, message: dict):
        """广播消息到所有连接"""
        if not self.active_connections:
            return

        data = json.dumps(message, default=str)
        disconnected = []

        async with self._lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

        # 清理断开的连接
        for conn in disconnected:
            await self.disconnect(conn)

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)


# 全局 WebSocket 管理器
ws_manager = ConnectionManager()


async def on_price_update(price_data: PriceData):
    """价格更新回调 - 检查告警 + WebSocket 广播 + 记录指标"""
    global _alerts_buffer
    assert price_data.timestamp is not None

    # 记录 Prometheus 指标
    record_price(price_data.price)
    update_ws_connections(ws_manager.connection_count)

    # 广播价格更新
    await ws_manager.broadcast(
        {
            "type": "price_update",
            "data": {
                "price": price_data.price,
                "currency": price_data.currency,
                "source": price_data.source,
                "timestamp": price_data.timestamp.isoformat(),
            },
        }
    )
    record_ws_message("price_update")

    # 检查告警
    if _alert_monitor:
        alerts = await _alert_monitor.check_price(price_data)
        if alerts:
            _alerts_buffer.extend(alerts)
            _alerts_buffer = _alerts_buffer[-100:]

            # 广播告警并记录指标
            for alert in alerts:
                record_alert(alert.alert_type.value)
                await ws_manager.broadcast(
                    {
                        "type": "alert",
                        "data": {
                            "alert_type": alert.alert_type.value,
                            "price": alert.price,
                            "message": alert.message,
                            "triggered_at": alert.triggered_at.isoformat(),
                        },
                    }
                )
                record_ws_message("alert")


async def _run_smart_analysis(model: Optional[str] = None) -> dict:
    """执行智能分析并缓存结果"""
    global _smart_analysis_cache

    async with _smart_analysis_lock:
        logger.info(f"开始执行智能分析... 模型: {model or '配置默认'}")
        try:
            # 获取配置
            manager = get_llm_config_manager()
            config = manager.reload_config()
            active_provider = config.get_active_provider()

            # 确定使用的模型
            use_model = model or config.active_model

            # 如果没有指定模型，尝试从 provider 的缓存模型列表中获取
            if not use_model and active_provider and active_provider.models:
                use_model = active_provider.models[0]
                logger.info(f"使用 provider 缓存的第一个模型: {use_model}")

            # 如果仍然没有模型，根据 provider 名称推断默认模型
            if not use_model and active_provider:
                provider_name = (active_provider.name or "").lower()
                if "deepseek" in provider_name:
                    use_model = "deepseek-chat"
                elif (
                    "qwen" in provider_name
                    or "通义" in provider_name
                    or "dashscope" in (active_provider.base_url or "").lower()
                ):
                    use_model = "qwen-turbo"
                elif "moonshot" in provider_name or "kimi" in provider_name:
                    use_model = "moonshot-v1-8k"
                elif "zhipu" in provider_name or "glm" in provider_name:
                    use_model = "glm-4"
                # 其他情况保持 None，让 provider 使用其默认值

            if (
                not active_provider
                or active_provider.id == "mock"
                or not active_provider.api_key
            ):
                # Mock 模式
                from .analyzer import MockLLMProvider

                mock_provider = MockLLMProvider()
                report = await mock_provider.smart_analyze()
                model_used = "Mock"
            else:
                # 真实 API 调用
                from .analyzer import LLMProvider, OpenAIProvider, AnthropicProvider

                provider: LLMProvider
                if (
                    "anthropic" in (active_provider.base_url or "").lower()
                    or "claude" in (active_provider.name or "").lower()
                ):
                    provider = AnthropicProvider(
                        api_key=active_provider.api_key, model=use_model
                    )
                    model_used = use_model or provider.model
                else:
                    provider = OpenAIProvider(
                        api_key=active_provider.api_key,
                        base_url=active_provider.base_url,
                        model=use_model,
                    )
                    model_used = use_model or provider.model

                report = await provider.smart_analyze()

            _smart_analysis_cache = {
                "title": report.title,
                "market_overview": report.market_overview,
                "recent_trend": report.recent_trend,
                "key_factors": report.key_factors,
                "price_prediction": report.price_prediction,
                "buy_timing": report.buy_timing,
                "recommendation": report.recommendation,
                "risk_warning": report.risk_warning,
                "generated_at": report.generated_at,
                "raw_response": report.raw_response,
                "model_used": model_used,
                "web_search_used": getattr(report, "web_search_used", False),
                "sources": getattr(report, "sources", []),
            }
            logger.info(f"智能分析完成，使用模型: {model_used}")
            return _smart_analysis_cache
        except Exception as e:
            logger.error(f"智能分析失败: {e}")
            raise


async def _daily_analysis_scheduler():
    """每天0点执行智能分析的调度器"""
    while True:
        try:
            # 计算距离下一个0点的秒数
            now = datetime.now()
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            seconds_until_midnight = (tomorrow - now).total_seconds()

            logger.info(
                f"智能分析定时任务：将在 {seconds_until_midnight / 3600:.1f} 小时后执行（明天0点）"
            )

            # 等待到0点
            await asyncio.sleep(seconds_until_midnight)

            # 执行分析
            logger.info("定时任务触发：开始执行每日智能分析")
            await _run_smart_analysis()
            logger.info("每日智能分析完成")

            # 等待1分钟，避免重复触发
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("智能分析定时任务已取消")
            break
        except Exception as e:
            logger.error(f"智能分析定时任务出错: {e}")
            # 出错后等待1小时再重试
            await asyncio.sleep(3600)
