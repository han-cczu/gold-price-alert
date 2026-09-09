"""Alert domain values."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AlertType(Enum):
    """告警类型"""

    THRESHOLD_UPPER = "threshold_upper"  # 突破上限
    THRESHOLD_LOWER = "threshold_lower"  # 跌破下限
    VOLATILITY = "volatility"  # 波动告警
    BREAKOUT_UP = "breakout_up"  # 向上突破
    BREAKOUT_DOWN = "breakout_down"  # 向下突破
    PULLBACK = "pullback"


@dataclass
class Alert:
    """告警信息"""

    alert_type: AlertType
    price: float
    message: str
    triggered_at: datetime
    change_percent: float | None = None
