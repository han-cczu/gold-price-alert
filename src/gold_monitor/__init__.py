"""金价实时监控与智能分析系统 - 纯 Web 模式"""

__version__ = "0.2.0"

from .config import settings
from .models import Database, GoldPrice, AlertRecord


def get_app():
    """获取 FastAPI 应用实例"""
    from .web import app

    return app


__all__ = [
    "settings",
    "Database",
    "GoldPrice",
    "AlertRecord",
    "get_app",
]
