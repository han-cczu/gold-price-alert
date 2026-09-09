"""Health, configuration, metrics and WebSocket HTTP adapters."""

import json
from fastapi import (
    APIRouter,
    Depends,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from ..dependencies import get_runtime
from ..schemas import HealthResponse
from ..metrics import get_metrics, get_metrics_content_type, update_db_records
from ..time_utils import utcnow, iso_utc

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    runtime = websocket.app.state.runtime
    if not runtime.started:
        await websocket.close(code=1013)
        return
    await runtime.ws.connect(websocket)
    try:
        price = runtime.collector.last_price
        if price:
            await runtime.ws.send(
                websocket,
                {
                    "type": "price_update",
                    "data": {
                        "price": price.price,
                        "currency": price.currency,
                        "source": price.source,
                        "timestamp": iso_utc(price.timestamp),
                        "recorded": price.recorded,
                    },
                },
            )
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if isinstance(message, dict) and message.get("type") == "ping":
                    await runtime.ws.send(websocket, {"type": "pong"})
            except json.JSONDecodeError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.ws.disconnect(websocket)


@router.get("/ws/status")
async def websocket_status(runtime=Depends(get_runtime)):
    return {"active_connections": runtime.ws.connection_count, "endpoint": "/ws"}


@router.get("/health", response_model=HealthResponse)
async def health_check(runtime=Depends(get_runtime)):
    status, last = "connected", None
    try:
        last = await runtime.db.run(runtime.db.get_latest_price)
    except Exception:
        status = "disconnected"
    collector = runtime.collector
    running = collector.is_running
    return HealthResponse(
        status="healthy" if status == "connected" and running else "unhealthy",
        database=status,
        data_source=runtime.config.data_source,
        data_source_healthy=bool(collector.last_price)
        and collector.stats.consecutive_failures == 0,
        collector_running=running,
        collector_stats=collector.stats.to_dict(),
        last_price=last.price if last else None,
        last_update=last.timestamp if last else None,
        uptime_seconds=(utcnow() - runtime.started_at).total_seconds(),
        fetch_interval=collector.get_config()["interval"],
    )


@router.get("/api/config")
async def get_config(runtime=Depends(get_runtime)):
    config = runtime.config
    llm = runtime.llm_config.get_config()
    active = llm.get_active_provider()
    return {
        "data_source": config.data_source,
        "fetch_interval": runtime.collector.get_config()["interval"],
        "alert_threshold_percent": config.alert_threshold_percent,
        "alert_price_upper": config.alert_price_upper,
        "alert_price_lower": config.alert_price_lower,
        "llm_provider": active.id if active else "mock",
        "llm_model": llm.active_model or None,
    }


@router.get("/metrics")
async def prometheus_metrics(runtime=Depends(get_runtime)):
    update_db_records(await runtime.db.run(runtime.db.get_price_count))
    return Response(content=get_metrics(), media_type=get_metrics_content_type())


@router.get("/api/security/status")
async def get_security_status(request: Request):
    config = request.app.state.settings
    return {
        "auth_enabled": config.enable_auth,
        "rate_limit_per_minute": config.rate_limit_per_minute,
        "admin_key_configured": bool(config.admin_api_key),
    }
