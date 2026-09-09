"""Alert orchestration and compatibility exports for domain/transport types."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from .config import Settings, settings
from .models import Database
from .data_sources.base import PriceData
from .alerts.types import Alert, AlertType
from .alerts.rules import VolatilityDetector, VolatilityResult
from .notifications.channels import (
    NotificationChannel,
    ConsoleNotification,
    EmailNotification,
    WebhookNotification,
    TelegramNotification,
)
from .notifications.delivery import NotificationManager

__all__ = [
    "Alert",
    "AlertType",
    "AlertMonitor",
    "VolatilityDetector",
    "VolatilityResult",
    "NotificationChannel",
    "ConsoleNotification",
    "EmailNotification",
    "WebhookNotification",
    "TelegramNotification",
    "NotificationManager",
]
logger = logging.getLogger(__name__)


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
        persist_interval: int = 60,  # 状态持久化间隔（秒）
        config: Settings | None = None,
    ):
        self._db = database
        self._lock = asyncio.Lock()
        config = config if config is not None else settings
        self._channels = (
            list(channels) if channels is not None else [ConsoleNotification()]
        )
        self._threshold_upper = (
            threshold_upper if threshold_upper is not None else config.alert_price_upper
        )
        self._threshold_lower = (
            threshold_lower if threshold_lower is not None else config.alert_price_lower
        )
        self._volatility_percent = (
            volatility_percent
            if volatility_percent is not None
            else config.alert_threshold_percent
        )
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
            max_amplitude=5.0,
        )

        # 通知管理器
        self._notification_manager = NotificationManager(database, self._channels)

        # 后台通知任务追踪（避免通知阻塞采集循环）
        self._pending_notifications: set[asyncio.Task] = set()

        # 状态持久化控制
        self._last_persist_time: datetime = datetime.now(timezone.utc).replace(
            tzinfo=None
        )
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
                    if (
                        state.alert_type == AlertType.VOLATILITY.value
                        and state.window_prices
                    ):
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
        if (
            not force
            and (now - self._last_persist_time).total_seconds() < self._persist_interval
        ):
            return

        try:
            # 冷却状态随告警事务提交；周期保存只更新波动窗口。
            if self._price_history:
                self._db.save_alert_state(
                    alert_type=AlertType.VOLATILITY.value,
                    window_prices=self._price_history,
                )

            self._last_persist_time = now
            self._state_dirty = False
            logger.debug("告警状态已持久化")
        except Exception as e:
            logger.error("持久化告警状态失败: %s", e)

    def _should_alert(self, alert_type: AlertType) -> bool:
        """检查是否应该发送告警（避免重复）"""
        last_time = self._last_alerts.get(alert_type)
        if (
            last_time
            and datetime.now(timezone.utc).replace(tzinfo=None) - last_time
            < self._alert_cooldown
        ):
            return False
        return True

    async def _record_alert(self, alert: Alert) -> int:
        record = await self._db.run(
            self._db.save_alert_with_state,
            alert.alert_type.value,
            alert.price,
            alert.message,
            triggered_at=alert.triggered_at,
            cooldown_until=alert.triggered_at + self._alert_cooldown,
        )
        self._last_alerts[alert.alert_type] = alert.triggered_at
        self._state_dirty = True
        return int(record.id)

    def _dispatch_notification(self, alert: Alert, record_id: int | None = None):
        """后台分发通知，避免通知重试/退避阻塞采集循环"""
        if not self._channels:
            return
        task = asyncio.create_task(
            self._notification_manager.send_with_retry(alert, record_id)
        )
        # 持有引用直到完成，避免任务被 GC 回收（"task was destroyed"）
        self._pending_notifications.add(task)
        task.add_done_callback(self._pending_notifications.discard)

    async def wait_pending_notifications(self, timeout: float = 15):
        """等待所有后台通知任务完成（用于测试与优雅关闭）"""
        if self._pending_notifications:
            tasks = list(self._pending_notifications)
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
        # Thresholds and history are denominated in USD per troy ounce.
        if price_data.currency != "USD":
            return []
        async with self._lock:
            task = asyncio.create_task(self._check_price(price_data))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish commit, publish cooldown and register delivery before shutdown.
                await task
                raise

    async def _check_price(self, price_data: PriceData) -> list[Alert]:
        """检查价格并触发告警"""
        alerts = []
        alert_ids: list[int] = []  # 与 alerts 一一对应的告警记录ID
        now = price_data.timestamp
        assert now is not None
        price = price_data.price

        # 更新价格历史
        self._update_price_history(price, now)

        # 检查上限告警
        if price >= self._threshold_upper and self._should_alert(
            AlertType.THRESHOLD_UPPER
        ):
            alert = Alert(
                alert_type=AlertType.THRESHOLD_UPPER,
                price=price,
                message=f"金价突破上限 ${self._threshold_upper:.2f}",
                triggered_at=now,
            )
            alerts.append(alert)
            alert_ids.append(await self._record_alert(alert))

        # 检查下限告警
        if price <= self._threshold_lower and self._should_alert(
            AlertType.THRESHOLD_LOWER
        ):
            alert = Alert(
                alert_type=AlertType.THRESHOLD_LOWER,
                price=price,
                message=f"金价跌破下限 ${self._threshold_lower:.2f}",
                triggered_at=now,
            )
            alerts.append(alert)
            alert_ids.append(await self._record_alert(alert))

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
                        change_percent=result.change_percent,
                    )
                    alerts.append(alert)
                    alert_ids.append(await self._record_alert(alert))
        else:
            # 使用简单的波动检测
            volatility = self._calculate_volatility()
            if volatility:
                change_percent, base_price = volatility
                if abs(
                    change_percent
                ) >= self._volatility_percent and self._should_alert(
                    AlertType.VOLATILITY
                ):
                    direction = "上涨" if change_percent > 0 else "下跌"
                    alert = Alert(
                        alert_type=AlertType.VOLATILITY,
                        price=price,
                        message=f"金价{self._volatility_window}分钟内{direction} {abs(change_percent):.2f}%",
                        triggered_at=now,
                        change_percent=change_percent,
                    )
                    alerts.append(alert)
                    alert_ids.append(await self._record_alert(alert))

        # 发送通知（后台分发，避免阻塞采集循环；通知失败重试不应拖慢价格采集）
        for alert, record_id in zip(alerts, alert_ids):
            self._dispatch_notification(alert, record_id)

        # 定期持久化状态
        await self._db.run(self._persist_state)

        return alerts

    def get_alert_history(self, limit: int = 50) -> list:
        """获取告警历史"""
        with self._db.get_session() as session:
            from .models import AlertRecord

            return (
                session.query(AlertRecord)
                .order_by(AlertRecord.triggered_at.desc())
                .limit(limit)
                .all()
            )

    def get_notification_logs(
        self, limit: int = 100, channel: str | None = None
    ) -> list:
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
