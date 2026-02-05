"""告警模块 - 价格监控与通知"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from .config import settings
from .models import Database
from .data_sources.base import PriceData


class AlertType(Enum):
    """告警类型"""
    THRESHOLD_UPPER = "threshold_upper"  # 突破上限
    THRESHOLD_LOWER = "threshold_lower"  # 跌破下限
    VOLATILITY = "volatility"            # 波动告警


@dataclass
class Alert:
    """告警信息"""
    alert_type: AlertType
    price: float
    message: str
    triggered_at: datetime
    change_percent: float | None = None


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

        console = Console()
        style = "red" if alert.alert_type in [AlertType.THRESHOLD_UPPER, AlertType.VOLATILITY] else "yellow"

        panel = Panel(
            f"[bold]{alert.message}[/bold]\n\n"
            f"当前价格: ${alert.price:.2f}\n"
            f"时间: {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}",
            title=f"⚠️ {alert.alert_type.value.upper()}",
            border_style=style
        )
        console.print(panel)
        return True


class EmailNotification(NotificationChannel):
    """邮件通知"""

    def __init__(self, smtp_host: str, smtp_port: int, username: str, password: str, to_addrs: list[str]):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to_addrs = to_addrs

    async def send(self, alert: Alert) -> bool:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        try:
            msg = MIMEMultipart()
            msg['From'] = self.username
            msg['To'] = ', '.join(self.to_addrs)
            msg['Subject'] = f"金价告警: {alert.alert_type.value}"

            body = f"""
金价告警通知

告警类型: {alert.alert_type.value}
当前价格: ${alert.price:.2f}
告警信息: {alert.message}
触发时间: {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}
"""
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.username, self.to_addrs, msg.as_string())

            return True
        except Exception as e:
            print(f"邮件发送失败: {e}")
            return False


class WebhookNotification(NotificationChannel):
    """Webhook 通知（支持钉钉、企业微信等）"""

    def __init__(self, webhook_url: str, webhook_type: str = "generic"):
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type

    async def send(self, alert: Alert) -> bool:
        import aiohttp

        try:
            if self.webhook_type == "dingtalk":
                payload = {
                    "msgtype": "text",
                    "text": {
                        "content": f"金价告警\n类型: {alert.alert_type.value}\n价格: ${alert.price:.2f}\n{alert.message}"
                    }
                }
            elif self.webhook_type == "wechat":
                payload = {
                    "msgtype": "text",
                    "text": {
                        "content": f"金价告警\n类型: {alert.alert_type.value}\n价格: ${alert.price:.2f}\n{alert.message}"
                    }
                }
            else:
                payload = {
                    "alert_type": alert.alert_type.value,
                    "price": alert.price,
                    "message": alert.message,
                    "triggered_at": alert.triggered_at.isoformat()
                }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            print(f"Webhook 发送失败: {e}")
            return False


class TelegramNotification(NotificationChannel):
    """Telegram Bot 通知"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, alert: Alert) -> bool:
        import aiohttp

        try:
            direction = "🔴" if alert.alert_type in [AlertType.THRESHOLD_LOWER] else "🟡"
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
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            print(f"Telegram 发送失败: {e}")
            return False


class AlertMonitor:
    """告警监控器"""

    def __init__(
        self,
        database: Database,
        channels: list[NotificationChannel] | None = None,
        threshold_upper: float | None = None,
        threshold_lower: float | None = None,
        volatility_percent: float | None = None,
        volatility_window_minutes: int = 5
    ):
        self._db = database
        self._channels = channels or [ConsoleNotification()]
        self._threshold_upper = threshold_upper or settings.alert_price_upper
        self._threshold_lower = threshold_lower or settings.alert_price_lower
        self._volatility_percent = volatility_percent or settings.alert_threshold_percent
        self._volatility_window = volatility_window_minutes

        # 记录最近的告警，避免重复告警
        self._last_alerts: dict[AlertType, datetime] = {}
        self._alert_cooldown = timedelta(minutes=5)

        # 价格历史（用于波动检测）
        self._price_history: list[tuple[datetime, float]] = []

    def _should_alert(self, alert_type: AlertType) -> bool:
        """检查是否应该发送告警（避免重复）"""
        last_time = self._last_alerts.get(alert_type)
        if last_time and datetime.utcnow() - last_time < self._alert_cooldown:
            return False
        return True

    def _record_alert(self, alert: Alert):
        """记录告警"""
        self._last_alerts[alert.alert_type] = alert.triggered_at
        self._db.save_alert(alert.alert_type.value, alert.price, alert.message)

    def _update_price_history(self, price: float, timestamp: datetime):
        """更新价格历史"""
        self._price_history.append((timestamp, price))
        # 只保留窗口期内的数据
        cutoff = timestamp - timedelta(minutes=self._volatility_window)
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

    def _calculate_volatility(self) -> tuple[float, float] | None:
        """计算波动率，返回 (变化百分比, 基准价格)"""
        if len(self._price_history) < 2:
            return None

        oldest_price = self._price_history[0][1]
        newest_price = self._price_history[-1][1]

        if oldest_price == 0:
            return None

        change_percent = ((newest_price - oldest_price) / oldest_price) * 100
        return change_percent, oldest_price

    async def check_price(self, price_data: PriceData) -> list[Alert]:
        """检查价格并触发告警"""
        alerts = []
        now = price_data.timestamp
        price = price_data.price

        # 更新价格历史
        self._update_price_history(price, now)

        # 检查上限告警
        if price >= self._threshold_upper and self._should_alert(AlertType.THRESHOLD_UPPER):
            alert = Alert(
                alert_type=AlertType.THRESHOLD_UPPER,
                price=price,
                message=f"金价突破上限 ${self._threshold_upper:.2f}",
                triggered_at=now
            )
            alerts.append(alert)
            self._record_alert(alert)

        # 检查下限告警
        if price <= self._threshold_lower and self._should_alert(AlertType.THRESHOLD_LOWER):
            alert = Alert(
                alert_type=AlertType.THRESHOLD_LOWER,
                price=price,
                message=f"金价跌破下限 ${self._threshold_lower:.2f}",
                triggered_at=now
            )
            alerts.append(alert)
            self._record_alert(alert)

        # 检查波动告警
        volatility = self._calculate_volatility()
        if volatility:
            change_percent, base_price = volatility
            if abs(change_percent) >= self._volatility_percent and self._should_alert(AlertType.VOLATILITY):
                direction = "上涨" if change_percent > 0 else "下跌"
                alert = Alert(
                    alert_type=AlertType.VOLATILITY,
                    price=price,
                    message=f"金价{self._volatility_window}分钟内{direction} {abs(change_percent):.2f}%",
                    triggered_at=now,
                    change_percent=change_percent
                )
                alerts.append(alert)
                self._record_alert(alert)

        # 发送通知
        for alert in alerts:
            for channel in self._channels:
                await channel.send(alert)

        return alerts

    def get_alert_history(self, limit: int = 50) -> list:
        """获取告警历史"""
        with self._db.get_session() as session:
            from .models import AlertRecord
            return session.query(AlertRecord).order_by(
                AlertRecord.triggered_at.desc()
            ).limit(limit).all()
