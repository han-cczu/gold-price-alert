"""金价实时监控与智能分析系统"""

__version__ = "0.1.0"

from .config import settings
from .models import Database, GoldPrice, AlertRecord
from .collector import DataCollector, create_data_source
from .alert import AlertMonitor, Alert, AlertType, ConsoleNotification
from .analyzer import GoldAnalyzer, AnalysisReport

__all__ = [
    "settings",
    "Database",
    "GoldPrice",
    "AlertRecord",
    "DataCollector",
    "create_data_source",
    "AlertMonitor",
    "Alert",
    "AlertType",
    "ConsoleNotification",
    "GoldAnalyzer",
    "AnalysisReport",
]


def get_app():
    """获取 FastAPI 应用实例（延迟导入）"""
    from .web import app
    return app
