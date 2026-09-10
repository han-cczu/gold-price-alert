"""Bounded retry and durable delivery outcomes for independent channels."""

import asyncio
import logging

from ..alerts.types import Alert
from ..models import Database
from .channels import NotificationChannel

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self, database: Database, channels=None):
        self._db = database
        self._channels = list(channels or [])
        self._max_retries = 3

    def set_channels(self, channels):
        self._channels = list(channels)

    def add_channel(self, channel):
        self._channels.append(channel)

    async def _send(self, channel: NotificationChannel, alert: Alert, alert_id):
        name = channel.__class__.__name__
        success, error, attempt = False, None, 0
        for attempt in range(self._max_retries):
            try:
                success = await asyncio.wait_for(channel.send(alert), timeout=12)
                if success:
                    break
                error = "渠道返回发送失败"
            except Exception as exc:
                error = type(exc).__name__
            if attempt < self._max_retries - 1:
                await asyncio.sleep(2**attempt)
        await self._db.run(
            self._db.save_notification_log,
            channel=name,
            status="success" if success else "failed",
            alert_id=alert_id,
            error_message=error if not success else None,
            retry_count=attempt,
        )
        return name, success

    async def send_with_retry(
        self, alert: Alert, alert_record_id=None
    ) -> dict[str, bool]:
        # Snapshot before awaiting: changing the configuration affects new deliveries.
        channels = list(self._channels)
        results = await asyncio.gather(
            *(self._send(channel, alert, alert_record_id) for channel in channels),
            return_exceptions=True,
        )
        outcomes = {}
        for channel, result in zip(channels, results):
            if isinstance(result, BaseException):
                logger.error(
                    "通知处理失败 (%s): %s",
                    channel.__class__.__name__,
                    type(result).__name__,
                )
                outcomes[channel.__class__.__name__] = False
            else:
                name, success = result
                outcomes[name] = success
        return outcomes
