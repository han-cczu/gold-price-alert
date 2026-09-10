"""Notification transport adapters."""

import asyncio
import logging
from abc import ABC, abstractmethod
from ..alerts.types import Alert, AlertType

logger = logging.getLogger(__name__)


class NotificationChannel(ABC):
    """通知渠道基类"""

    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """发送告警通知"""
        pass


class ConsoleNotification(NotificationChannel):
    """控制台通知（用于测试和CLI）"""

    async def send(self, alert: Alert) -> bool:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(safe_box=True)
        style = (
            "red"
            if alert.alert_type in [AlertType.THRESHOLD_UPPER, AlertType.VOLATILITY]
            else "yellow"
        )

        panel = Panel(
            f"[bold]{alert.message}[/bold]\n\n"
            f"当前价格: ${alert.price:.2f}\n"
            f"时间: {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}",
            title=f"ALERT {alert.alert_type.value.upper()}",
            border_style=style,
        )
        try:
            console.print(panel)
        except UnicodeEncodeError as e:
            logger.warning("控制台编码不支持告警内容，跳过控制台渲染: %s", e)
        return True


class EmailNotification(NotificationChannel):
    """邮件通知"""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to_addrs: list[str],
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to_addrs = to_addrs

    async def send(self, alert: Alert) -> bool:
        task = asyncio.create_task(asyncio.to_thread(self._send, alert))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Drain it before retry/close so a
            # timed-out SMTP send never overlaps its replacement attempt.
            await task
            raise

    def _send(self, alert: Alert) -> bool:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        try:
            msg = MIMEMultipart()
            msg["From"] = self.username
            msg["To"] = ", ".join(self.to_addrs)
            msg["Subject"] = f"金价告警: {alert.alert_type.value}"

            body = f"""
金价告警通知

告警类型: {alert.alert_type.value}
当前价格: ${alert.price:.2f}
告警信息: {alert.message}
触发时间: {alert.triggered_at.strftime("%Y-%m-%d %H:%M:%S")}
"""
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.username, self.to_addrs, msg.as_string())

            return True
        except Exception as e:
            logger.warning("邮件发送失败: %s", type(e).__name__)
            return False


class WebhookNotification(NotificationChannel):
    """Webhook 通知（支持钉钉、企业微信等）"""

    def __init__(self, webhook_url: str, webhook_type: str = "generic"):
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type

    async def send(self, alert: Alert) -> bool:
        import aiohttp

        try:
            payload: dict[str, object]
            if self.webhook_type == "dingtalk":
                payload = {
                    "msgtype": "text",
                    "text": {
                        "content": f"金价告警\n类型: {alert.alert_type.value}\n价格: ${alert.price:.2f}\n{alert.message}"
                    },
                }
            elif self.webhook_type == "wechat":
                payload = {
                    "msgtype": "text",
                    "text": {
                        "content": f"金价告警\n类型: {alert.alert_type.value}\n价格: ${alert.price:.2f}\n{alert.message}"
                    },
                }
            else:
                payload = {
                    "alert_type": alert.alert_type.value,
                    "price": alert.price,
                    "message": alert.message,
                    "triggered_at": alert.triggered_at.isoformat(),
                }

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.warning("Webhook 发送失败: %s", type(e).__name__)
            return False


class TelegramNotification(NotificationChannel):
    """Telegram Bot 通知"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, alert: Alert) -> bool:
        import aiohttp

        try:
            direction = (
                "🔴" if alert.alert_type in [AlertType.THRESHOLD_LOWER] else "🟡"
            )
            if alert.alert_type == AlertType.THRESHOLD_UPPER:
                direction = "🔴"
            elif alert.alert_type == AlertType.VOLATILITY:
                direction = "⚡"

            text = (
                f"{direction} *金价告警*\n\n"
                f"类型: `{alert.alert_type.value}`\n"
                f"价格: `${alert.price:.2f}`\n"
                f"信息: {alert.message}\n"
                f"时间: {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.warning("Telegram 发送失败: %s", type(e).__name__)
            return False
