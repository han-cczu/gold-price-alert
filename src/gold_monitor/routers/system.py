"""系统级路由：/health、/metrics、/api/config、/api/security/status、/ws、/ws/status"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect

from ..config import settings
from ..collector import create_data_source, get_collector
from ..llm_config import get_llm_config_manager
from ..schemas import HealthResponse
from ..state import db, ws_manager, get_auth, get_app_start_time
from ..metrics import (
    get_metrics, get_metrics_content_type, update_db_records
)

router = APIRouter()


# ============ WebSocket 端点 ============

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 实时价格推送

    消息格式:
    - 价格更新: {"type": "price_update", "data": {...}}
    - 告警通知: {"type": "alert", "data": {...}}
    - 心跳: {"type": "ping"} -> {"type": "pong"}
    """
    await ws_manager.connect(websocket)

    # 连接成功后发送当前价格
    collector = get_collector()
    if collector and collector.last_price:
        await websocket.send_json({
            "type": "price_update",
            "data": {
                "price": collector.last_price.price,
                "currency": collector.last_price.currency,
                "source": collector.last_price.source,
                "timestamp": collector.last_price.timestamp.isoformat()
            }
        })

    try:
        while True:
            # 接收客户端消息（心跳检测）
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)


@router.get("/ws/status")
async def websocket_status():
    """WebSocket 连接状态"""
    return {
        "active_connections": ws_manager.connection_count,
        "endpoint": "/ws"
    }


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    last_record = db.get_latest_price()
    collector = get_collector()

    # 检查数据源健康状态
    data_source_healthy = False
    try:
        source = create_data_source()
        data_source_healthy = await source.health_check()
    except Exception:
        data_source_healthy = False

    # 检查数据库连接
    db_status = "connected"
    try:
        from sqlalchemy import text
        with db.get_session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"

    # 计算运行时间
    uptime = (datetime.now(timezone.utc).replace(tzinfo=None) - get_app_start_time()).total_seconds()

    # 检查采集器状态
    collector_running = collector is not None and collector.is_running
    collector_stats = collector.stats.to_dict() if collector else None

    # 判断整体健康状态
    status = "healthy" if db_status == "connected" and collector_running else "unhealthy"

    return HealthResponse(
        status=status,
        database=db_status,
        data_source=settings.data_source,
        data_source_healthy=data_source_healthy,
        collector_running=collector_running,
        collector_stats=collector_stats,
        last_price=last_record.price if last_record else None,
        last_update=last_record.timestamp if last_record else None,
        uptime_seconds=uptime,
        fetch_interval=settings.fetch_interval
    )


@router.get("/api/config")
async def get_config():
    """获取当前配置（脱敏）"""
    llm_config = get_llm_config_manager().get_config()
    active_provider = llm_config.get_active_provider()
    return {
        "data_source": settings.data_source,
        "fetch_interval": settings.fetch_interval,
        "alert_threshold_percent": settings.alert_threshold_percent,
        "alert_price_upper": settings.alert_price_upper,
        "alert_price_lower": settings.alert_price_lower,
        # 返回当前激活的平台 ID（不返回 API Key）
        "llm_provider": active_provider.id if active_provider else (llm_config.active_provider_id or "mock"),
        "llm_model": llm_config.active_model or None
    }


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标端点"""
    # 更新数据库记录数
    try:
        count = db.get_price_count()
        update_db_records(count)
    except Exception:
        pass

    return Response(
        content=get_metrics(),
        media_type=get_metrics_content_type()
    )


@router.get("/api/security/status")
async def get_security_status():
    """获取安全配置状态"""
    auth = get_auth()
    return {
        "auth_enabled": settings.enable_auth,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "admin_key_configured": bool(settings.admin_api_key),
        "admin_key_hint": auth.admin_key[:8] + "..." if auth.admin_key else None
    }
