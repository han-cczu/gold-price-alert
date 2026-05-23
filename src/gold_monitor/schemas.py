"""Pydantic 请求/响应模型集中定义

从 web.py 抽离，供各 routers 共用。字段、默认值、类型保持与原 web.py 完全一致。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PriceResponse(BaseModel):
    price: float
    currency: str = "USD"
    source: str
    timestamp: datetime


class PriceHistoryResponse(BaseModel):
    data: list[PriceResponse]
    count: int
    stats: dict


class ChartDataResponse(BaseModel):
    timestamps: list[str]
    prices: list[float]
    current_price: float
    price_change: float
    price_change_percent: float
    high: float
    low: float


class AlertResponse(BaseModel):
    id: int
    alert_type: str
    price: float
    message: str
    triggered_at: datetime


class AnalysisResponse(BaseModel):
    summary: str
    possible_reasons: list[str]
    market_sentiment: str
    recommendation: str
    generated_at: datetime


class HealthResponse(BaseModel):
    status: str
    database: str
    data_source: str
    data_source_healthy: bool
    collector_running: bool
    collector_stats: Optional[dict]
    last_price: Optional[float]
    last_update: Optional[datetime]
    uptime_seconds: Optional[float]
    fetch_interval: int


class ExchangeRateResponse(BaseModel):
    usd_cny: float
    updated_at: datetime


class BankPriceResponse(BaseModel):
    bank_name: str
    bank_code: str
    buy_price: float
    sell_price: float
    timestamp: datetime
    product_name: str


class BankPricesResponse(BaseModel):
    data: list[BankPriceResponse]
    base_price_cny: float
    london_gold_cny: float
    updated_at: datetime


class ProviderRequest(BaseModel):
    """平台配置请求"""
    name: str
    base_url: str
    api_key: Optional[str] = None


class ProviderUpdateRequest(BaseModel):
    """平台更新请求"""
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class SetActiveRequest(BaseModel):
    """设置当前使用的平台和模型"""
    provider_id: str
    model: Optional[str] = None


class SmartAnalysisResponse(BaseModel):
    """智能分析响应"""
    title: str
    market_overview: str
    recent_trend: str
    key_factors: list[str]
    price_prediction: str
    buy_timing: str
    recommendation: str
    risk_warning: str
    generated_at: datetime
    is_cached: bool = False
    cache_age_minutes: Optional[int] = None
    model_used: Optional[str] = None
    raw_response: Optional[str] = None  # AI 原始响应，可用于调试或完整查看
    web_search_used: bool = False  # 本次是否真正联网搜索
    sources: list[dict] = []  # 引用来源 [{url, title}]


class RefreshAnalysisRequest(BaseModel):
    """刷新分析请求"""
    model: Optional[str] = None


class ProviderProbeRequest(BaseModel):
    """探测请求（获取模型列表）：允许携带表单中尚未保存的 key/url。

    api_key/base_url 为空或脱敏（含 "..."）时，后端回退到已保存的平台配置。
    """
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class TestConnectionRequest(BaseModel):
    """测试连接请求：允许携带表单中尚未保存的 key/url。"""
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class NotificationConfigRequest(BaseModel):
    """通知渠道配置请求"""
    enabled: Optional[bool] = None
    config: Optional[dict] = None


class NotificationTestRequest(BaseModel):
    """通知测试请求"""
    test_message: Optional[str] = None
