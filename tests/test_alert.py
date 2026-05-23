"""告警模块测试"""

import shutil
import tempfile

import pytest
from datetime import datetime, timedelta, timezone

from gold_monitor.alert import (
    AlertMonitor, Alert, AlertType,
    ConsoleNotification, NotificationChannel, VolatilityDetector
)
from gold_monitor.data_sources.base import PriceData
from gold_monitor.models import Database


class MockNotification(NotificationChannel):
    """模拟通知渠道（用于测试）"""

    def __init__(self):
        self.sent_alerts = []

    async def send(self, alert: Alert) -> bool:
        self.sent_alerts.append(alert)
        return True


@pytest.fixture
def mock_db():
    """创建临时数据库"""
    tmp_dir = tempfile.mkdtemp()
    db_path = f"{tmp_dir}/test.db"
    db = Database(f"sqlite:///{db_path}")
    db.create_tables()
    yield db
    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture
def mock_notification():
    """创建模拟通知渠道"""
    return MockNotification()


@pytest.fixture
def alert_monitor(mock_db, mock_notification):
    """创建告警监控器"""
    return AlertMonitor(
        database=mock_db,
        channels=[mock_notification],
        threshold_upper=2100.0,
        threshold_lower=1900.0,
        volatility_percent=1.0,
        volatility_window_minutes=5
    )


@pytest.mark.asyncio
async def test_threshold_upper_alert(alert_monitor, mock_notification):
    """测试价格突破上限告警"""
    price_data = PriceData(
        price=2150.0,  # 超过上限 2100
        currency="USD",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        source="test"
    )

    alerts = await alert_monitor.check_price(price_data)
    # 通知现在是后台分发，断言前需等待其完成
    await alert_monitor.wait_pending_notifications()

    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.THRESHOLD_UPPER
    assert alerts[0].price == 2150.0
    assert len(mock_notification.sent_alerts) == 1


@pytest.mark.asyncio
async def test_threshold_lower_alert(alert_monitor, mock_notification):
    """测试价格跌破下限告警"""
    price_data = PriceData(
        price=1850.0,  # 低于下限 1900
        currency="USD",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        source="test"
    )

    alerts = await alert_monitor.check_price(price_data)

    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.THRESHOLD_LOWER
    assert alerts[0].price == 1850.0


@pytest.mark.asyncio
async def test_no_alert_in_range(alert_monitor, mock_notification):
    """测试价格在正常范围内不触发告警"""
    price_data = PriceData(
        price=2000.0,  # 在 1900-2100 范围内
        currency="USD",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        source="test"
    )

    alerts = await alert_monitor.check_price(price_data)

    assert len(alerts) == 0
    assert len(mock_notification.sent_alerts) == 0


@pytest.mark.asyncio
async def test_volatility_alert(alert_monitor, mock_notification):
    """测试波动告警"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 构建连续上涨趋势（智能波动检测器要求 >=3 个连续同向采样）
    trend_prices = [
        (2000.0, -4),  # 基准价
        (2006.0, -3),  # +0.3%
        (2012.0, -2),  # +0.6%
        (2019.0, -1),  # +0.95%
        (2025.0,  0),  # +1.25% 总涨幅
    ]

    all_alerts = []
    for price, minutes_offset in trend_prices:
        pd = PriceData(price=price, timestamp=now + timedelta(minutes=minutes_offset), source="test")
        alerts = await alert_monitor.check_price(pd)
        all_alerts.extend(alerts)

    # 应该触发波动相关告警（VOLATILITY 或 BREAKOUT_UP 等）
    volatility_types = {AlertType.VOLATILITY, AlertType.BREAKOUT_UP, AlertType.BREAKOUT_DOWN, AlertType.PULLBACK}
    volatility_alerts = [a for a in all_alerts if a.alert_type in volatility_types]
    assert len(volatility_alerts) >= 1


@pytest.mark.asyncio
async def test_alert_cooldown(alert_monitor, mock_notification):
    """测试告警冷却期（避免重复告警）"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 第一次突破上限
    price1 = PriceData(price=2150.0, timestamp=now, source="test")
    alerts1 = await alert_monitor.check_price(price1)
    assert len(alerts1) == 1

    # 短时间内再次突破，应该不触发（冷却期内）
    price2 = PriceData(price=2160.0, timestamp=now + timedelta(seconds=30), source="test")
    alerts2 = await alert_monitor.check_price(price2)

    # 只有第一次触发的告警
    threshold_alerts = [a for a in alerts2 if a.alert_type == AlertType.THRESHOLD_UPPER]
    assert len(threshold_alerts) == 0


@pytest.mark.asyncio
async def test_notification_log_links_alert_id(alert_monitor, mock_notification, mock_db):
    """通知日志应正确关联告警记录ID（修复前 alert_id 恒为 NULL）"""
    from gold_monitor.models import AlertRecord

    price_data = PriceData(
        price=2150.0,
        currency="USD",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        source="test",
    )

    await alert_monitor.check_price(price_data)
    await alert_monitor.wait_pending_notifications()

    with mock_db.get_session() as session:
        rec = session.query(AlertRecord).filter(
            AlertRecord.alert_type == "threshold_upper"
        ).first()
        assert rec is not None
        rec_id = rec.id

    logs = mock_db.get_notification_logs()
    assert len(logs) >= 1
    assert any(log.alert_id == rec_id for log in logs)


# ============ VolatilityDetector 抗噪多条件 ============

def _series(values):
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    return [(base + timedelta(minutes=i), v) for i, v in enumerate(values)]


def test_volatility_detector_too_few_points_returns_none():
    det = VolatilityDetector()
    assert det.analyze(_series([2000.0])) is None


def test_volatility_detector_alerts_on_clean_trend():
    """涨幅达标 + 连续同向 + 振幅可控 -> 触发"""
    det = VolatilityDetector(threshold_percent=1.0, min_consecutive=3, max_amplitude=5.0)
    result = det.analyze(_series([2000.0, 2010.0, 2020.0, 2030.0]))
    assert det.should_alert(result) is True


def test_volatility_detector_below_threshold_no_alert():
    det = VolatilityDetector(threshold_percent=1.0, min_consecutive=3, max_amplitude=5.0)
    result = det.analyze(_series([2000.0, 2002.0, 2004.0, 2006.0]))  # 仅 +0.3%
    assert det.should_alert(result) is False


def test_volatility_detector_rejects_high_amplitude_noise():
    """涨幅达标但剧烈震荡（大振幅）-> 视为噪声不报警"""
    det = VolatilityDetector(threshold_percent=1.0, min_consecutive=3, max_amplitude=5.0)
    result = det.analyze(_series([2000.0, 2200.0, 1900.0, 2030.0]))  # 振幅 ~15%
    assert det.should_alert(result) is False


def test_volatility_detector_rejects_insufficient_consecutive():
    """涨幅达标但方向反复（连续同向不足）-> 不报警"""
    det = VolatilityDetector(threshold_percent=1.0, min_consecutive=3, max_amplitude=50.0)
    result = det.analyze(_series([2000.0, 2031.0, 2030.0, 2030.5]))
    assert result.consecutive_trend < 3
    assert det.should_alert(result) is False


@pytest.mark.asyncio
async def test_console_notification():
    """测试控制台通知"""
    notification = ConsoleNotification()

    alert = Alert(
        alert_type=AlertType.THRESHOLD_UPPER,
        price=2150.0,
        message="测试告警",
        triggered_at=datetime.now(timezone.utc).replace(tzinfo=None)
    )

    result = await notification.send(alert)
    assert result is True
