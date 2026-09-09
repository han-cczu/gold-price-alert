"""Analysis inputs and report contracts."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AnalysisContext:
    """分析上下文"""

    current_price: float
    price_change: float
    price_change_percent: float
    time_window_minutes: int
    recent_prices: list[tuple[datetime, float]]
    additional_info: dict[str, Any] | None = None


@dataclass
class AnalysisReport:
    """分析报告"""

    summary: str
    possible_reasons: list[str]
    market_sentiment: str
    recommendation: str
    generated_at: datetime
    raw_response: str


@dataclass
class SmartAnalysisReport:
    """智能分析报告（网络搜索版）"""

    title: str  # 标题
    market_overview: str  # 市场概况
    recent_trend: str  # 近期走势
    key_factors: list[str]  # 影响因素
    price_prediction: str  # 价格预测
    buy_timing: str  # 买入时机
    recommendation: str  # 操作建议
    risk_warning: str  # 风险提示
    generated_at: datetime
    raw_response: str
    web_search_used: bool = False  # 本次是否真正联网搜索
    sources: list = field(default_factory=list)  # 引用来源 [{url, title}]
