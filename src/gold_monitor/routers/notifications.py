"""Notification HTTP adapter; configuration and delivery share one service."""

from typing import Optional
from fastapi import APIRouter, Depends, Query
from ..dependencies import get_runtime, require_admin_dep
from ..schemas import NotificationConfigRequest, NotificationTestRequest
from ..time_utils import iso_utc

router = APIRouter(dependencies=[Depends(require_admin_dep)])


@router.get("/api/notifications/config")
async def get_notification_configs(runtime=Depends(get_runtime)):
    return {"configs": await runtime.notifications.list_configs()}


@router.get("/api/notifications/config/{channel}")
async def get_notification_config(channel: str, runtime=Depends(get_runtime)):
    return await runtime.notifications.get(channel)


@router.put("/api/notifications/config/{channel}")
async def update_notification_config(
    channel: str, request: NotificationConfigRequest, runtime=Depends(get_runtime)
):
    return await runtime.notifications.update(
        channel, enabled=request.enabled, config=request.config
    )


@router.get("/api/notifications/logs")
async def get_notification_logs(
    limit: int = Query(100, ge=1, le=500),
    channel: Optional[str] = None,
    runtime=Depends(get_runtime),
):
    logs = await runtime.db.run(
        runtime.db.get_notification_logs, limit=limit, channel=channel
    )
    return {
        "logs": [
            {
                "id": r.id,
                "alert_id": r.alert_id,
                "channel": r.channel,
                "status": r.status,
                "error_message": r.error_message,
                "retry_count": r.retry_count,
                "sent_at": iso_utc(r.sent_at),
            }
            for r in logs
        ],
        "count": len(logs),
    }


@router.post("/api/notifications/test/{channel}")
async def test_notification(
    channel: str,
    request: Optional[NotificationTestRequest] = None,
    runtime=Depends(get_runtime),
):
    return await runtime.notifications.test(
        channel,
        request.test_message
        if request and request.test_message
        else "这是一条测试通知",
    )
