"""告警模块 - 价格监控与通知"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional
import statistics

from .config import settings
from .models import Database
from .data_sources.base import PriceData

logger = logging.getLogger(__name__)


class AlertType(Enum):
    """告警类型"""
    THRESHOLD_UPPER = "threshold_upper"  # 突破上限
    THRESHOLD_LOWER = "threshold_lower"  # 跌破下限
    VOLATILITY = "volatility"            # 波动告警
    BREAKOUT_UP = "breakout_up"          # 向上突破
    BREAKOUT_DOWN = "breakout_down"      # 向下突破
    PULLBACK = "pullback"                # 回落


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


# ============ 波动检测器 ============

@dataclass
class VolatilityResult:
    """波动分析结果"""
    change_percent: float           # 首尾涨跌幅
    amplitude_percent: float        # 振幅 (high-low)/base
    rolling_std: float              # 滚动标准差
    consecutive_trend: int          # 连续同向次数
    event_type: str                 # breakout_up/breakout_down/pullback/normal


class VolatilityDetector:
    """波动检测器 - 多维度分析，降低噪声误报"""

    def __init__(
        self,
        threshold_percent: float = 1.0,
        min_consecutive: int = 3,
        max_amplitude: float = 5.0
    ):
        self._threshold = threshold_percent
        self._min_consecutive = min_consecutive
        self._max_amplitude = max_amplitude

    def analyze(self, prices: list[tuple[datetime, float]]) -> Optional[VolatilityResult]:
        """分析价格序列，返回波动结果"""
        if len(prices) < 2:
            return None

        price_values = [p for _, p in prices]
        base_price = price_values[0]
        current_price = price_values[-1]

        if base_price == 0:
            return None

        # 1. 计算首尾涨跌幅
        change_percent = ((current_price - base_price) / base_price) * 100

        # 2. 计算振幅
        high = max(price_values)
        low = min(price_values)
        amplitude_percent = ((high - low) / base_price) * 100

        # 3. 计算滚动标准差
        rolling_std = 0.0
        if len(price_values) >= 3:
            rolling_std = statistics.stdev(price_values)

        # 4. 计算连续同向趋势
        consecutive_trend = self._calc_consecutive_trend(price_values)

        # 5. 检测事件类型
        event_type = self._detect_event(price_values, change_percent)

        return VolatilityResult(
            change_percent=change_percent,
            amplitude_percent=amplitude_percent,
            rolling_std=rolling_std,
            consecutive_trend=consecutive_trend,
            event_type=event_type
        )

    def _calc_consecutive_trend(self, prices: list[float]) -> int:
        """计算连续同向趋势次数"""
        if len(prices) < 2:
            return 0

        consecutive = 1
        last_direction = None

        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff == 0:
                continue

            current_direction = 1 if diff > 0 else -1

            if last_direction is None:
                last_direction = current_direction
                consecutive = 1
            elif current_direction == last_direction:
                consecutive += 1
            else:
                last_direction = current_direction
                consecutive = 1

        return consecutive

    def _detect_event(self, prices: list[float], change_percent: float) -> str:
        """检测事件类型"""
        if len(prices) < 3:
            return "normal"

        # 检测是否为突破（最后几个价格连续创新高/新低）
        recent = prices[-3:]
        if all(recent[i] > recent[i - 1] for i in range(1, len(recent))) and change_percent > 0:
            return "breakout_up"
        if all(recent[i] < recent[i - 1] for i in range(1, len(recent))) and change_percent < 0:
            return "breakout_down"

        # 检测回落（先涨后跌或先跌后涨）
        mid_idx = len(prices) // 2
        first_half = prices[:mid_idx]
        second_half = prices[mid_idx:]

        if first_half and second_half:
            first_trend = sum(1 for i in range(1, len(first_half)) if first_half[i] > first_half[i - 1])
            second_trend = sum(1 for i in range(1, len(second_half)) if second_half[i] > second_half[i - 1])

            # 前半段涨、后半段跌，或者反过来
            if first_trend > len(first_half) // 2 and second_trend < len(second_half) // 2:
                return "pullback"
            if first_trend < len(first_half) // 2 and second_trend > len(second_half) // 2:
                return "pullback"

        return "normal"

    def should_alert(self, result: VolatilityResult) -> bool:
        """多条件联合判断，降低误报"""
        if result is None:
            return False

        # 基本条件：涨跌幅超过阈值
        if abs(result.change_percent) < self._threshold:
            return False

        # 抗噪条件1：至少连续N次采样确认趋势
        if result.consecutive_trend < self._min_consecutive:
            return False

        # 抗噪条件2：排除剧烈震荡（振幅过大说明是噪声）
        if result.amplitude_percent > self._max_amplitude:
            return False

        return True


# ============ 通知管理器 ============

class NotificationManager:
    """通知管理器 - 统一管理所有渠道，支持重试"""

    def __init__(self, database: Database, channels: list[NotificationChannel] = None):
        self._db = database
        self._channels = channels or []
        self._max_retries = 3

    def set_channels(self, channels: list[NotificationChannel]):
        """设置通知渠道"""
        self._channels = channels

    def add_channel(self, channel: NotificationChannel):
        """添加通知渠道"""
        self._channels.append(channel)

    async def send_with_retry(self, alert: Alert, alert_record_id: int = None) -> dict[str, bool]:
        """发送通知到所有渠道，支持重试
        
        返回: {channel_name: success}
        """
        results = {}

        for channel in self._channels:
            channel_name = channel.__class__.__name__
            success = False
            last_error = None

            for attempt in range(self._max_retries):
                try:
                    success = await channel.send(alert)
                    if success:
                        break
                except Exception as e:
                    last_error = str(e)
                    logger.warning(
                        "通知发送失败 (渠道: %s, 尝试: %d/%d): %s",
                        channel_name, attempt + 1, self._max_retries, e
                    )

                if not success and attempt < self._max_retries - 1:
                    # 指数退避
                    await asyncio.sleep(2 ** attempt)

            # 记录发送结果
            self._db.save_notification_log(
                channel=channel_name,
                status="success" if success else "failed",
                alert_id=alert_record_id,
                error_message=last_error if not success else None,
                retry_count=attempt if not success else 0
            )

            results[channel_name] = success
            logger.info(
                "通知发送 %s (渠道: %s, 告警: %s)",
                "成功" if success else "失败", channel_name, alert.alert_type.value
            )

        return results


class AlertMonitor:
    """告警监控器 - 支持状态持久化"""

    def __init__(
        self,
        database: Database,
        channels: list[NotificationChannel] | None = None,
        threshold_upper: float | None = None,
        threshold_lower: float | None = None,
        volatility_percent: float | None = None,
        volatility_window_minutes: int = 5,
        use_smart_volatility: bool = True,
        persist_interval: int = 60  # 状态持久化间隔（秒）
    ):
        self._db = database
        self._channels = channels or [ConsoleNotification()]
        self._threshold_upper = threshold_upper or settings.alert_price_upper
        self._threshold_lower = threshold_lower or settings.alert_price_lower
        self._volatility_percent = volatility_percent or settings.alert_threshold_percent
        self._volatility_window = volatility_window_minutes
        self._use_smart_volatility = use_smart_volatility
        self._persist_interval = persist_interval

        # 记录最近的告警，避免重复告警
        self._last_alerts: dict[AlertType, datetime] = {}
        self._alert_cooldown = timedelta(minutes=5)

        # 价格历史（用于波动检测）
        self._price_history: list[tuple[datetime, float]] = []

        # 波动检测器
        self._volatility_detector = VolatilityDetector(
            threshold_percent=self._volatility_percent,
            min_consecutive=3,
            max_amplitude=5.0
        )

        # 通知管理器
        self._notification_manager = NotificationManager(database, self._channels)

        # 状态持久化控制
        self._last_persist_time: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
        self._state_dirty = False

        # 启动时恢复状态
        self._restore_state()

    def _restore_state(self):
        """从数据库恢复告警状态"""
        try:
            states = self._db.get_all_alert_states()
            for state in states:
                try:
                    alert_type = AlertType(state.alert_type)
                    if state.last_triggered_at:
                        self._last_alerts[alert_type] = state.last_triggered_at

                    # 恢复价格历史（仅用于波动检测）
                    if state.alert_type == AlertType.VOLATILITY.value and state.window_prices:
                        restored_prices = state.get_window_prices()
                        if restored_prices:
                            self._price_history = restored_prices
                            logger.info("恢复价格历史: %d 条记录", len(restored_prices))

                except ValueError:
                    continue

            logger.info("告警状态恢复完成: %d 个状态", len(states))
        except Exception as e:
            logger.warning("恢复告警状态失败: %s", e)

    def _persist_state(self, force: bool = False):
        """持久化告警状态到数据库"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # 检查是否需要持久化
        if not force and not self._state_dirty:
            return
        if not force and (now - self._last_persist_time).total_seconds() < self._persist_interval:
            return

        try:
            # 持久化各告警类型的状态
            for alert_type, last_triggered in self._last_alerts.items():
                cooldown_until = last_triggered + self._alert_cooldown if last_triggered else None
                self._db.save_alert_state(
                    alert_type=alert_type.value,
                    last_triggered_at=last_triggered,
                    cooldown_until=cooldown_until
                )

            # 持久化价格历史（用于波动检测）
            if self._price_history:
                self._db.save_alert_state(
                    alert_type=AlertType.VOLATILITY.value,
                    window_prices=self._price_history
                )

            self._last_persist_time = now
            self._state_dirty = False
            logger.debug("告警状态已持久化")
        except Exception as e:
            logger.error("持久化告警状态失败: %s", e)

    def _should_alert(self, alert_type: AlertType) -> bool:
        """检查是否应该发送告警（避免重复）"""
        last_time = self._last_alerts.get(alert_type)
        if last_time and datetime.now(timezone.utc).replace(tzinfo=None) - last_time < self._alert_cooldown:
            return False
        return True

    def _record_alert(self, alert: Alert) -> int:
        """记录告警，返回告警记录ID"""
        self._last_alerts[alert.alert_type] = alert.triggered_at
        self._state_dirty = True

        # 保存到数据库
        record = self._db.save_alert(alert.alert_type.value, alert.price, alert.message)

        # 同时更新告警状态
        self._db.save_alert_state(
            alert_type=alert.alert_type.value,
            last_triggered_at=alert.triggered_at,
            cooldown_until=alert.triggered_at + self._alert_cooldown
        )

        return record.id

    def _update_price_history(self, price: float, timestamp: datetime):
        """更新价格历史"""
        self._price_history.append((timestamp, price))
        # 只保留窗口期内的数据
        cutoff = timestamp - timedelta(minutes=self._volatility_window)
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]
        self._state_dirty = True

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
            alert_id = self._record_alert(alert)

        # 检查下限告警
        if price <= self._threshold_lower and self._should_alert(AlertType.THRESHOLD_LOWER):
            alert = Alert(
                alert_type=AlertType.THRESHOLD_LOWER,
                price=price,
                message=f"金价跌破下限 ${self._threshold_lower:.2f}",
                triggered_at=now
            )
            alerts.append(alert)
            alert_id = self._record_alert(alert)

        # 检查波动告警（使用智能或简单算法）
        if self._use_smart_volatility:
            # 使用抗噪波动检测器
            result = self._volatility_detector.analyze(self._price_history)
            if result and self._volatility_detector.should_alert(result):
                # 根据事件类型选择告警类型
                if result.event_type == "breakout_up":
                    alert_type = AlertType.BREAKOUT_UP
                    message = f"金价向上突破! {self._volatility_window}分钟内上涨 {abs(result.change_percent):.2f}%"
                elif result.event_type == "breakout_down":
                    alert_type = AlertType.BREAKOUT_DOWN
                    message = f"金价向下突破! {self._volatility_window}分钟内下跌 {abs(result.change_percent):.2f}%"
                elif result.event_type == "pullback":
                    alert_type = AlertType.PULLBACK
                    direction = "上涨" if result.change_percent > 0 else "下跌"
                    message = f"金价回落修正! {self._volatility_window}分钟内{direction} {abs(result.change_percent):.2f}%"
                else:
                    alert_type = AlertType.VOLATILITY
                    direction = "上涨" if result.change_percent > 0 else "下跌"
                    message = f"金价{self._volatility_window}分钟内{direction} {abs(result.change_percent):.2f}%"

                if self._should_alert(alert_type):
                    alert = Alert(
                        alert_type=alert_type,
                        price=price,
                        message=message,
                        triggered_at=now,
                        change_percent=result.change_percent
                    )
                    alerts.append(alert)
                    alert_id = self._record_alert(alert)
        else:
            # 使用简单的波动检测
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
                    alert_id = self._record_alert(alert)

        # 发送通知（使用通知管理器，支持重试）
        for alert in alerts:
            if self._channels:
                # 获取告警记录ID
                await self._notification_manager.send_with_retry(alert)

        # 定期持久化状态
        self._persist_state()

        return alerts

    def get_alert_history(self, limit: int = 50) -> list:
        """获取告警历史"""
        with self._db.get_session() as session:
            from .models import AlertRecord
            return session.query(AlertRecord).order_by(
                AlertRecord.triggered_at.desc()
            ).limit(limit).all()

    def get_notification_logs(self, limit: int = 100, channel: str = None) -> list:
        """获取通知发送日志"""
        return self._db.get_notification_logs(limit=limit, channel=channel)

    def set_channels(self, channels: list[NotificationChannel]):
        """设置通知渠道"""
        self._channels = channels
        self._notification_manager.set_channels(channels)

    def add_channel(self, channel: NotificationChannel):
        """添加通知渠道"""
        self._channels.append(channel)
        self._notification_manager.add_channel(channel)

    def force_persist(self):
        """强制持久化状态"""
        self._persist_state(force=True)
