"""数据库模型定义"""

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Float, DateTime, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow():
    """获取当前 UTC 时间（naive datetime，兼容 SQLAlchemy）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GoldPrice(Base):
    """金价记录表"""

    __tablename__ = "gold_prices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="金价 (USD/oz)")
    currency: Mapped[str] = mapped_column(String(10), default="USD", comment="货币单位")
    source: Mapped[str] = mapped_column(String(50), comment="数据来源")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, comment="采集时间"
    )

    def __repr__(self):
        return (
            f"<GoldPrice(id={self.id}, price={self.price}, timestamp={self.timestamp})>"
        )


class AlertRecord(Base):
    """告警记录表"""

    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="告警类型: threshold/volatility"
    )
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="触发时价格")
    message: Mapped[str] = mapped_column(Text, comment="告警消息")
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, comment="触发时间"
    )

    def __repr__(self):
        return (
            f"<AlertRecord(id={self.id}, type={self.alert_type}, price={self.price})>"
        )


class AlertState(Base):
    """告警状态持久化表 - 用于服务重启后恢复状态"""

    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, comment="告警类型"
    )
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="上次触发时间"
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="冷却结束时间"
    )
    base_price: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="波动计算基准价"
    )
    window_prices: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON序列化的价格序列"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, comment="更新时间"
    )

    def __repr__(self):
        return f"<AlertState(type={self.alert_type}, last_triggered={self.last_triggered_at})>"

    def get_window_prices(self) -> list[tuple[datetime, float]]:
        """解析价格序列JSON"""
        if not self.window_prices:
            return []
        try:
            data = json.loads(self.window_prices)
            return [(datetime.fromisoformat(t), p) for t, p in data]
        except (json.JSONDecodeError, ValueError):
            return []

    def set_window_prices(self, prices: list[tuple[datetime, float]]):
        """序列化价格序列为JSON"""
        data = [(t.isoformat(), p) for t, p in prices]
        self.window_prices = json.dumps(data)


class NotificationLog(Base):
    """通知发送记录表"""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_records.id"), nullable=True, comment="关联的告警ID"
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="通知渠道: email/webhook/telegram"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="状态: success/failed/pending"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误信息"
    )
    retry_count: Mapped[int] = mapped_column(default=0, comment="重试次数")
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, comment="发送时间"
    )

    def __repr__(self):
        return f"<NotificationLog(channel={self.channel}, status={self.status})>"


class NotificationConfig(Base):
    """通知渠道配置表 - Web可配置"""

    __tablename__ = "notification_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_type: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        comment="渠道类型: email/webhook/telegram",
    )
    enabled: Mapped[bool] = mapped_column(default=False, comment="是否启用")
    config: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON配置（不含敏感密钥）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, comment="更新时间"
    )

    def __repr__(self):
        return f"<NotificationConfig(type={self.channel_type}, enabled={self.enabled})>"

    def get_config(self) -> dict:
        """解析配置JSON"""
        if not self.config:
            return {}
        try:
            return json.loads(self.config)
        except json.JSONDecodeError:
            return {}

    def set_config(self, config: dict):
        """序列化配置为JSON"""
        self.config = json.dumps(config, ensure_ascii=False)


class AnalysisRecord(Base):
    """AI分析记录表 - 分析结果可追溯"""

    __tablename__ = "analysis_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    analysis_type: Mapped[str] = mapped_column(
        String(50), comment="分析类型: volatility/smart"
    )
    model_provider: Mapped[str | None] = mapped_column(String(50), comment="模型提供商")
    model_name: Mapped[str | None] = mapped_column(String(100), comment="模型名称")
    price_range_start: Mapped[datetime | None] = mapped_column(
        DateTime, comment="分析数据起始时间"
    )
    price_range_end: Mapped[datetime | None] = mapped_column(
        DateTime, comment="分析数据结束时间"
    )
    input_summary: Mapped[str | None] = mapped_column(Text, comment="输入数据摘要")
    result: Mapped[str | None] = mapped_column(Text, comment="分析结果JSON")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, comment="创建时间"
    )

    def __repr__(self):
        return f"<AnalysisRecord(type={self.analysis_type}, created={self.created_at})>"

    def get_result(self) -> dict:
        """解析分析结果JSON"""
        if not self.result:
            return {}
        try:
            return json.loads(self.result)
        except json.JSONDecodeError:
            return {}

    def set_result(self, result: dict):
        """序列化分析结果为JSON"""
        self.result = json.dumps(result, ensure_ascii=False)


if TYPE_CHECKING:
    from .storage.database import Database as Database


def __getattr__(name: str):
    """Keep the historical models.Database import without a circular dependency."""
    if name == "Database":
        from .storage.database import Database

        return Database
    raise AttributeError(name)
