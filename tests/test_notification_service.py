"""One effective configuration for saved settings, probes and live alerts."""

import pytest
import asyncio
import threading
from datetime import timedelta

from gold_monitor.alert import AlertMonitor
from gold_monitor.data_sources.base import PriceData
from gold_monitor.models import Database
from gold_monitor.services.notifications import NotificationService
from gold_monitor.notifications.delivery import NotificationManager
from gold_monitor.notifications.channels import EmailNotification
from gold_monitor.alerts.types import Alert, AlertType
from gold_monitor.time_utils import utcnow


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'notifications.db').as_posix()}")
    database.create_tables()
    yield database
    database.close()


@pytest.mark.asyncio
async def test_saved_settings_drive_probe_and_actual_alert(db, app_settings):
    sent = []

    class FakeNotification:
        def __init__(self, values):
            self.values = dict(values)

        async def send(self, alert):
            sent.append((self.values, alert.message))
            return True

    service = NotificationService(
        db, app_settings, factory=lambda name, values: FakeNotification(values)
    )
    monitor = AlertMonitor(db, channels=[], threshold_upper=2100)
    service.monitor = monitor
    await service.update("console", enabled=False)
    await service.update(
        "webhook", enabled=True, config={"webhook_url": "https://example.invalid/hook"}
    )
    assert (await service.test("webhook", "probe"))["success"] is True
    await monitor.check_price(PriceData(2200, source="test"))
    await monitor.wait_pending_notifications()
    assert len(sent) == 2
    assert sent[0][0] == sent[1][0]
    assert sent[1][0]["webhook_url"] == "https://example.invalid/hook"
    await service.update("webhook", enabled=False)
    assert (await service.test("webhook", "disabled"))["success"] is False
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_invalid_channel_update_preserves_config(db, app_settings):
    service = NotificationService(db, app_settings)
    with pytest.raises(ValueError):
        await service.update("webhook", enabled=True, config={"webhook_url": "invalid"})
    assert db.get_notification_config("webhook") is None


@pytest.mark.asyncio
async def test_masked_secrets_are_preserved_and_encrypted(db, app_settings):
    service = NotificationService(
        db, app_settings.model_copy(update={"secret_key": "stable-test-secret"})
    )
    await service.update(
        "telegram", enabled=True, config={"bot_token": "test-token", "chat_id": "123"}
    )
    assert (
        db.get_notification_config("telegram")
        .get_config()["bot_token"]
        .startswith("enc:")
    )
    assert (await service.get("telegram"))["config"]["bot_token"] == "****"
    await service.update("telegram", config={"bot_token": "****", "chat_id": "456"})
    channels = await service.active_channels()
    assert channels[-1].bot_token == "test-token"
    assert channels[-1].chat_id == "456"


@pytest.mark.asyncio
async def test_failed_alert_transaction_does_not_advance_cooldown(db, monkeypatch):
    monitor = AlertMonitor(db, channels=[], threshold_upper=2100)
    original = db.save_alert_with_state

    def fail(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(db, "save_alert_with_state", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        await monitor.check_price(PriceData(2200, source="test"))
    assert not monitor._last_alerts
    assert db.get_alerts() == []
    monkeypatch.setattr(db, "save_alert_with_state", original)
    assert len(await monitor.check_price(PriceData(2200, source="test"))) == 1


@pytest.mark.asyncio
async def test_invalid_secret_or_encryption_failure_never_persists(
    db, app_settings, monkeypatch
):
    service = NotificationService(
        db, app_settings.model_copy(update={"secret_key": "test-key"})
    )
    with pytest.raises(ValueError, match="字符串"):
        await service.update("email", enabled=False, config={"password": 123})
    assert db.get_notification_config("email") is None
    monkeypatch.setattr(service._secret, "encrypt", lambda value: "")
    with pytest.raises(ValueError, match="加密失败"):
        await service.update(
            "email", enabled=False, config={"password": "test-password"}
        )
    assert db.get_notification_config("email") is None


@pytest.mark.asyncio
async def test_log_failure_waits_for_all_channel_tasks(db, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class Fast:
        async def send(self, alert):
            return True

    class Slow:
        async def send(self, alert):
            started.set()
            await release.wait()
            return True

    original = db.save_notification_log

    def save(**kwargs):
        if kwargs["channel"] == "Fast":
            raise RuntimeError("log failed")
        return original(**kwargs)

    monkeypatch.setattr(db, "save_notification_log", save)
    manager = NotificationManager(db, [Fast(), Slow()])
    task = asyncio.create_task(
        manager.send_with_retry(Alert(AlertType.VOLATILITY, 2000, "test", utcnow()))
    )
    await started.wait()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    assert await task == {"Fast": False, "Slow": True}


@pytest.mark.asyncio
async def test_cancelled_smtp_send_drains_thread_before_return(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    notifier = EmailNotification("test", 587, "test", "", ["test@example.invalid"])

    def send(alert):
        entered.set()
        release.wait(timeout=2)
        return True

    monkeypatch.setattr(notifier, "_send", send)
    task = asyncio.create_task(
        notifier.send(Alert(AlertType.VOLATILITY, 2000, "test", utcnow()))
    )
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_during_alert_commit_preserves_cooldown_and_delivery(
    db, monkeypatch
):
    previous = utcnow() - timedelta(minutes=10)
    db.save_alert_state("threshold_upper", last_triggered_at=previous)
    delivered = []

    class Channel:
        async def send(self, alert):
            delivered.append(alert)
            return True

    monitor = AlertMonitor(db, channels=[Channel()], threshold_upper=2100)
    entered, release = threading.Event(), threading.Event()
    original = db.save_alert_with_state

    def delayed_commit(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "save_alert_with_state", delayed_commit)
    sample = PriceData(2200, source="test")
    task = asyncio.create_task(monitor.check_price(sample))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await monitor.wait_pending_notifications()
    await db.run(monitor.force_persist)
    assert len(db.get_alerts()) == 1
    assert len(delivered) == 1
    assert monitor._last_alerts[AlertType.THRESHOLD_UPPER] == sample.timestamp
    restored = AlertMonitor(db, channels=[], threshold_upper=2100)
    assert await restored.check_price(sample) == []


@pytest.mark.asyncio
async def test_non_usd_quote_does_not_change_usd_alert_history(db):
    monitor = AlertMonitor(db, channels=[], threshold_upper=2100)
    assert (
        await monitor.check_price(PriceData(16000, currency="CNY", source="test")) == []
    )
    assert monitor._price_history == []
    assert db.get_alerts() == []
