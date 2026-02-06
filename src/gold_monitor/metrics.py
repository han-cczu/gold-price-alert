"""Prometheus Metrics 模块 - 可观测性指标"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试导入 prometheus_client，如果未安装则使用模拟实现
try:
    from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus_client 未安装，指标功能将被禁用")

    # 模拟实现
    class MockMetric:
        def labels(self, *args, **kwargs):
            return self
        def inc(self, *args, **kwargs):
            pass
        def dec(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            pass
        def observe(self, *args, **kwargs):
            pass
        def info(self, *args, **kwargs):
            pass

    def Counter(*args, **kwargs):
        return MockMetric()

    def Gauge(*args, **kwargs):
        return MockMetric()

    def Histogram(*args, **kwargs):
        return MockMetric()

    def Info(*args, **kwargs):
        return MockMetric()

    def generate_latest():
        return b"# Prometheus metrics disabled - prometheus_client not installed\n"

    CONTENT_TYPE_LATEST = "text/plain"


# ============ 指标定义 ============

# 数据采集指标
FETCH_TOTAL = Counter(
    "gold_fetch_total",
    "Total number of price fetch attempts",
    ["source", "status"]  # status: success, failure, timeout
)

FETCH_LATENCY = Histogram(
    "gold_fetch_latency_seconds",
    "Price fetch latency in seconds",
    ["source"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
)

# 价格指标
CURRENT_PRICE = Gauge(
    "gold_current_price_usd",
    "Current gold price in USD per troy ounce"
)

PRICE_CHANGE_PERCENT = Gauge(
    "gold_price_change_percent",
    "Price change percentage in the last window"
)

# 告警指标
ALERT_TOTAL = Counter(
    "gold_alert_total",
    "Total number of alerts triggered",
    ["type"]  # type: threshold_upper, threshold_lower, volatility, etc.
)

ALERT_ACTIVE = Gauge(
    "gold_alert_active",
    "Number of currently active alerts"
)

# 通知指标
NOTIFICATION_TOTAL = Counter(
    "gold_notification_total",
    "Total number of notifications sent",
    ["channel", "status"]  # channel: email, webhook, telegram; status: success, failure
)

NOTIFICATION_LATENCY = Histogram(
    "gold_notification_latency_seconds",
    "Notification send latency in seconds",
    ["channel"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
)

# WebSocket 指标
WS_CONNECTIONS = Gauge(
    "gold_websocket_connections",
    "Number of active WebSocket connections"
)

WS_MESSAGES_TOTAL = Counter(
    "gold_websocket_messages_total",
    "Total WebSocket messages sent",
    ["type"]  # type: price_update, alert, pong
)

# API 指标
API_REQUEST_TOTAL = Counter(
    "gold_api_requests_total",
    "Total API requests",
    ["method", "endpoint", "status"]
)

API_REQUEST_LATENCY = Histogram(
    "gold_api_request_latency_seconds",
    "API request latency in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
)

# 数据库指标
DB_RECORDS_TOTAL = Gauge(
    "gold_db_records_total",
    "Total number of price records in database"
)

DB_OPERATION_LATENCY = Histogram(
    "gold_db_operation_latency_seconds",
    "Database operation latency in seconds",
    ["operation"],  # operation: insert, select, delete
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0)
)

# 分析指标
ANALYSIS_TOTAL = Counter(
    "gold_analysis_total",
    "Total number of AI analyses performed",
    ["type", "provider"]  # type: volatility, smart; provider: openai, anthropic, etc.
)

ANALYSIS_LATENCY = Histogram(
    "gold_analysis_latency_seconds",
    "AI analysis latency in seconds",
    ["type", "provider"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0)
)

# 系统信息
SYSTEM_INFO = Info(
    "gold_monitor",
    "Gold monitor system information"
)


# ============ 辅助函数 ============

def record_fetch(source: str, success: bool, latency_seconds: float):
    """记录数据采集"""
    status = "success" if success else "failure"
    FETCH_TOTAL.labels(source=source, status=status).inc()
    if success:
        FETCH_LATENCY.labels(source=source).observe(latency_seconds)


def record_price(price: float, change_percent: float = None):
    """记录当前价格"""
    CURRENT_PRICE.set(price)
    if change_percent is not None:
        PRICE_CHANGE_PERCENT.set(change_percent)


def record_alert(alert_type: str):
    """记录告警"""
    ALERT_TOTAL.labels(type=alert_type).inc()


def record_notification(channel: str, success: bool, latency_seconds: float = None):
    """记录通知发送"""
    status = "success" if success else "failure"
    NOTIFICATION_TOTAL.labels(channel=channel, status=status).inc()
    if latency_seconds is not None:
        NOTIFICATION_LATENCY.labels(channel=channel).observe(latency_seconds)


def update_ws_connections(count: int):
    """更新 WebSocket 连接数"""
    WS_CONNECTIONS.set(count)


def record_ws_message(message_type: str):
    """记录 WebSocket 消息"""
    WS_MESSAGES_TOTAL.labels(type=message_type).inc()


def record_api_request(method: str, endpoint: str, status_code: int, latency_seconds: float):
    """记录 API 请求"""
    # 简化 endpoint（去掉参数）
    endpoint = endpoint.split("?")[0]
    if len(endpoint) > 50:
        endpoint = endpoint[:50] + "..."

    status = str(status_code)
    API_REQUEST_TOTAL.labels(method=method, endpoint=endpoint, status=status).inc()
    API_REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(latency_seconds)


def update_db_records(count: int):
    """更新数据库记录数"""
    DB_RECORDS_TOTAL.set(count)


def record_db_operation(operation: str, latency_seconds: float):
    """记录数据库操作"""
    DB_OPERATION_LATENCY.labels(operation=operation).observe(latency_seconds)


def record_analysis(analysis_type: str, provider: str, latency_seconds: float):
    """记录 AI 分析"""
    ANALYSIS_TOTAL.labels(type=analysis_type, provider=provider).inc()
    ANALYSIS_LATENCY.labels(type=analysis_type, provider=provider).observe(latency_seconds)


def set_system_info(version: str, data_source: str, fetch_interval: int):
    """设置系统信息"""
    if PROMETHEUS_AVAILABLE:
        SYSTEM_INFO.info({
            "version": version,
            "data_source": data_source,
            "fetch_interval": str(fetch_interval)
        })


# ============ 指标导出 ============

def get_metrics() -> bytes:
    """获取所有指标（Prometheus 格式）"""
    return generate_latest()


def get_metrics_content_type() -> str:
    """获取指标内容类型"""
    return CONTENT_TYPE_LATEST


# ============ 中间件辅助 ============

class MetricsMiddleware:
    """FastAPI 中间件 - 自动记录 API 请求指标"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        status_code = 500  # 默认状态码

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency = time.time() - start_time
            method = scope.get("method", "UNKNOWN")
            path = scope.get("path", "/")

            # 排除指标端点本身
            if path != "/metrics":
                record_api_request(method, path, status_code, latency)
