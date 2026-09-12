"""Resolve, validate and apply the same configuration for every delivery path."""

import asyncio
from urllib.parse import urlparse

from ..alerts.types import Alert, AlertType
from ..config import Settings
from ..errors import ConfigurationError, InvalidInput
from ..models import Database
from ..notifications.channels import (
    ConsoleNotification,
    EmailNotification,
    WebhookNotification,
    TelegramNotification,
)
from ..notifications.delivery import NotificationManager
from ..security import SecretManager
from ..time_utils import iso_utc, utcnow

CHANNELS = ("console", "email", "webhook", "telegram")
SECRET_FIELDS = {"password", "bot_token", "webhook_url"}


class NotificationService:
    def __init__(self, database: Database, config: Settings, *, factory=None):
        self.db, self.config = database, config
        self.factory = factory or self._build_channel
        self.monitor = None
        self._lock = asyncio.Lock()
        self._secret = SecretManager(config.secret_key) if config.secret_key else None

    def _defaults(self, channel):
        c = self.config
        defaults = {
            "console": (True, {}),
            "email": (
                bool(c.smtp_host and c.smtp_username and c.alert_email_to),
                {
                    "smtp_host": c.smtp_host,
                    "smtp_port": c.smtp_port,
                    "username": c.smtp_username,
                    "password": c.smtp_password,
                    "to_addrs": [
                        s.strip() for s in c.alert_email_to.split(",") if s.strip()
                    ],
                },
            ),
            "webhook": (
                bool(c.webhook_url),
                {
                    "webhook_url": c.webhook_url,
                    "webhook_type": c.webhook_type,
                },
            ),
            "telegram": (
                bool(c.telegram_bot_token and c.telegram_chat_id),
                {
                    "bot_token": c.telegram_bot_token,
                    "chat_id": c.telegram_chat_id,
                },
            ),
        }
        if channel not in defaults:
            raise InvalidInput("不支持的通知渠道")
        return defaults[channel]

    def _decode(self, values):
        values = dict(values)
        for key in SECRET_FIELDS & values.keys():
            value = values[key]
            if isinstance(value, str) and value.startswith("enc:"):
                if self._secret is None:
                    raise ConfigurationError("通知配置需要原有主密钥解密")
                values[key] = self._secret.decrypt(value[4:])
                if not values[key]:
                    raise ConfigurationError("通知配置解密失败")
        return values

    def _encode(self, values):
        values = dict(values)
        for key in SECRET_FIELDS & values.keys():
            if values[key] and self._secret:
                encrypted = self._secret.encrypt(values[key])
                if not encrypted:
                    raise ConfigurationError("通知配置加密失败，原有配置未改变")
                values[key] = "enc:" + encrypted
        return values

    def _effective(self, channel, record):
        enabled, values = self._defaults(channel)
        if record is not None:
            enabled = record.enabled
            values.update(self._decode(record.get_config()))
        return enabled, values

    @staticmethod
    def _build_channel(channel, values):
        if channel == "console":
            return ConsoleNotification()
        required = {
            "email": ("smtp_host", "username", "to_addrs"),
            "webhook": ("webhook_url",),
            "telegram": ("bot_token", "chat_id"),
        }[channel]
        if any(not values.get(key) for key in required):
            raise InvalidInput(f"{channel} 通知配置不完整")
        if channel == "email":
            if (
                not isinstance(values.get("smtp_port"), int)
                or not 1 <= values["smtp_port"] <= 65535
            ):
                raise InvalidInput("SMTP 端口必须在 1–65535 之间")
            if not isinstance(values["to_addrs"], list) or not all(
                isinstance(v, str) and "@" in v for v in values["to_addrs"]
            ):
                raise InvalidInput("收件人必须是邮箱列表")
            return EmailNotification(**values)
        if channel == "webhook":
            url = urlparse(values["webhook_url"])
            if url.scheme not in ("http", "https") or not url.netloc:
                raise InvalidInput("Webhook 地址必须是 HTTP(S) URL")
            if values.get("webhook_type") not in ("generic", "dingtalk", "wechat"):
                raise InvalidInput("不支持的 Webhook 类型")
            return WebhookNotification(**values)
        return TelegramNotification(**values)

    async def active_channels(self):
        records = await self.db.run(self.db.get_all_notification_configs)
        by_name = {record.channel_type: record for record in records}
        channels = []
        for name in CHANNELS:
            record = by_name.get(name)
            if record is not None and not record.enabled:
                continue
            enabled, values = self._effective(name, by_name.get(name))
            if enabled:
                channels.append(self.factory(name, values))
        return channels

    async def get(self, channel):
        record = await self.db.run(self.db.get_notification_config, channel)
        enabled, values = self._effective(channel, record)
        safe = {
            key: ("****" if key in SECRET_FIELDS and value else value)
            for key, value in values.items()
        }
        return {
            "channel_type": channel,
            "enabled": enabled,
            "config": safe,
            "updated_at": iso_utc(record.updated_at) if record else None,
        }

    async def list_configs(self):
        return [await self.get(channel) for channel in CHANNELS]

    async def update(self, channel, *, enabled=None, config=None):
        async with self._lock:
            record = await self.db.run(self.db.get_notification_config, channel)
            current_enabled, current = self._effective(channel, record)
            overrides = self._decode(record.get_config()) if record else {}
            patch = dict(config or {})
            if set(patch) - set(current):
                raise InvalidInput("包含不支持的通知配置字段")
            for key, value in patch.items():
                if key in SECRET_FIELDS and (not value or value == "****"):
                    continue
                overrides[key] = value
            for key, value in overrides.items():
                if key == "smtp_port":
                    if type(value) is not int or not 1 <= value <= 65535:
                        raise InvalidInput("SMTP 端口必须在 1–65535 之间")
                elif key == "to_addrs":
                    if not isinstance(value, list) or not all(
                        isinstance(v, str) and "@" in v for v in value
                    ):
                        raise InvalidInput("收件人必须是邮箱列表")
                elif not isinstance(value, str):
                    raise InvalidInput(f"{key} 必须是字符串")
            current.update(overrides)
            effective_enabled = current_enabled if enabled is None else enabled
            # Validate before persistence and before changing the running monitor.
            if effective_enabled:
                self.factory(channel, current)
            await self.db.run(
                self.db.save_notification_config,
                channel_type=channel,
                enabled=effective_enabled,
                config=self._encode(overrides),
            )
            if self.monitor:
                self.monitor.set_channels(await self.active_channels())
            return {"success": True, **await self.get(channel)}

    async def test(self, channel, message):
        record = await self.db.run(self.db.get_notification_config, channel)
        enabled, values = self._effective(channel, record)
        if not enabled:
            return {"channel": channel, "success": False, "message": "渠道未启用"}
        notifier = self.factory(channel, values)
        delivery = NotificationManager(self.db, [notifier])
        alert = Alert(AlertType.VOLATILITY, 2000.0, message, utcnow())
        result = await delivery.send_with_retry(alert)
        success = all(result.values())
        return {
            "channel": channel,
            "success": success,
            "message": "通知发送成功" if success else "通知发送失败",
        }
