"""通知配置相关路由：/api/notifications/*"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from ..config import settings
from ..schemas import NotificationConfigRequest, NotificationTestRequest
from ..state import db

router = APIRouter()


@router.get("/api/notifications/config")
async def get_notification_configs():
    """获取所有通知渠道配置"""
    configs = db.get_all_notification_configs()
    return {
        "configs": [
            {
                "channel_type": c.channel_type,
                "enabled": c.enabled,
                "config": c.get_config(),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in configs
        ]
    }


@router.get("/api/notifications/config/{channel}")
async def get_notification_config(channel: str):
    """获取指定通知渠道配置"""
    config = db.get_notification_config(channel)
    if not config:
        return {
            "channel_type": channel,
            "enabled": False,
            "config": {},
            "message": "渠道未配置",
        }
    return {
        "channel_type": config.channel_type,
        "enabled": config.enabled,
        "config": config.get_config(),
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


@router.put("/api/notifications/config/{channel}")
async def update_notification_config(channel: str, request: NotificationConfigRequest):
    """更新通知渠道配置"""
    config = db.save_notification_config(
        channel_type=channel, enabled=request.enabled, config=request.config
    )
    return {
        "success": True,
        "channel_type": config.channel_type,
        "enabled": config.enabled,
        "config": config.get_config(),
    }


@router.get("/api/notifications/logs")
async def get_notification_logs(
    limit: int = Query(default=100, le=500), channel: Optional[str] = None
):
    """获取通知发送日志"""
    logs = db.get_notification_logs(limit=limit, channel=channel)
    return {
        "logs": [
            {
                "id": log.id,
                "alert_id": log.alert_id,
                "channel": log.channel,
                "status": log.status,
                "error_message": log.error_message,
                "retry_count": log.retry_count,
                "sent_at": log.sent_at.isoformat() if log.sent_at else None,
            }
            for log in logs
        ],
        "count": len(logs),
    }


@router.post("/api/notifications/test/{channel}")
async def test_notification(
    channel: str, request: Optional[NotificationTestRequest] = None
):
    """测试通知发送"""
    from ..alert import (
        Alert,
        AlertType,
        ConsoleNotification,
        EmailNotification,
        WebhookNotification,
        TelegramNotification,
        NotificationChannel,
    )

    test_message = (
        request.test_message if request and request.test_message else "这是一条测试通知"
    )

    # 创建测试告警
    test_alert = Alert(
        alert_type=AlertType.VOLATILITY,
        price=2000.0,
        message=test_message,
        triggered_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    result = {"channel": channel, "success": False, "message": ""}

    try:
        if channel == "console":
            notification: NotificationChannel = ConsoleNotification()
            success = await notification.send(test_alert)
            result["success"] = success
            result["message"] = "控制台通知已发送（查看服务器日志）"

        elif channel == "email":
            if not settings.smtp_host or not settings.smtp_username:
                result["message"] = "邮件未配置（SMTP_HOST, SMTP_USERNAME）"
            else:
                notification = EmailNotification(
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    to_addrs=settings.alert_email_to.split(",")
                    if settings.alert_email_to
                    else [],
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = "邮件发送成功" if success else "邮件发送失败"

        elif channel == "webhook":
            if not settings.webhook_url:
                result["message"] = "Webhook 未配置"
            else:
                notification = WebhookNotification(
                    webhook_url=settings.webhook_url, webhook_type=settings.webhook_type
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = (
                    "Webhook 发送成功" if success else "Webhook 发送失败"
                )

        elif channel == "telegram":
            if not settings.telegram_bot_token or not settings.telegram_chat_id:
                result["message"] = "Telegram 未配置（BOT_TOKEN, CHAT_ID）"
            else:
                notification = TelegramNotification(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = (
                    "Telegram 发送成功" if success else "Telegram 发送失败"
                )

        else:
            result["message"] = f"不支持的通知渠道: {channel}"

    except Exception as e:
        result["message"] = f"发送失败: {str(e)}"

    # 记录日志
    db.save_notification_log(
        channel=channel,
        status="success" if result["success"] else "failed",
        error_message=str(result["message"]) if not result["success"] else None,
    )

    return result
