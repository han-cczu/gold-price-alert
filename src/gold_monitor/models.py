"""数据库模型定义"""

import json
from datetime import datetime, timezone
from sqlalchemy import create_engine, Float, DateTime, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


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
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, comment="采集时间")

    def __repr__(self):
        return f"<GoldPrice(id={self.id}, price={self.price}, timestamp={self.timestamp})>"


class AlertRecord(Base):
    """告警记录表"""
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="告警类型: threshold/volatility")
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="触发时价格")
    message: Mapped[str] = mapped_column(Text, comment="告警消息")
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, comment="触发时间")

    def __repr__(self):
        return f"<AlertRecord(id={self.id}, type={self.alert_type}, price={self.price})>"


class AlertState(Base):
    """告警状态持久化表 - 用于服务重启后恢复状态"""
    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="告警类型")
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="上次触发时间")
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="冷却结束时间")
    base_price: Mapped[float | None] = mapped_column(Float, nullable=True, comment="波动计算基准价")
    window_prices: Mapped[str | None] = mapped_column(Text, nullable=True, comment="JSON序列化的价格序列")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, comment="更新时间")

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
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alert_records.id"), nullable=True, comment="关联的告警ID")
    channel: Mapped[str] = mapped_column(String(50), nullable=False, comment="通知渠道: email/webhook/telegram")
    status: Mapped[str] = mapped_column(String(20), nullable=False, comment="状态: success/failed/pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    retry_count: Mapped[int] = mapped_column(default=0, comment="重试次数")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, comment="发送时间")

    def __repr__(self):
        return f"<NotificationLog(channel={self.channel}, status={self.status})>"


class NotificationConfig(Base):
    """通知渠道配置表 - Web可配置"""
    __tablename__ = "notification_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="渠道类型: email/webhook/telegram")
    enabled: Mapped[bool] = mapped_column(default=False, comment="是否启用")
    config: Mapped[str | None] = mapped_column(Text, nullable=True, comment="JSON配置（不含敏感密钥）")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, comment="更新时间")

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
    analysis_type: Mapped[str] = mapped_column(String(50), comment="分析类型: volatility/smart")
    model_provider: Mapped[str | None] = mapped_column(String(50), comment="模型提供商")
    model_name: Mapped[str | None] = mapped_column(String(100), comment="模型名称")
    price_range_start: Mapped[datetime | None] = mapped_column(DateTime, comment="分析数据起始时间")
    price_range_end: Mapped[datetime | None] = mapped_column(DateTime, comment="分析数据结束时间")
    input_summary: Mapped[str | None] = mapped_column(Text, comment="输入数据摘要")
    result: Mapped[str | None] = mapped_column(Text, comment="分析结果JSON")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, comment="创建时间")

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


class Database:
    """数据库管理类"""

    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def create_tables(self):
        """创建所有表"""
        Base.metadata.create_all(self.engine)

    def get_session(self):
        """获取数据库会话"""
        return self.SessionLocal()

    def save_price(self, price: float, source: str = "mock") -> GoldPrice:
        """保存金价记录"""
        with self.get_session() as session:
            record = GoldPrice(price=price, source=source)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_latest_price(self) -> GoldPrice | None:
        """获取最新金价"""
        with self.get_session() as session:
            return session.query(GoldPrice).order_by(GoldPrice.timestamp.desc()).first()

    def get_recent_prices(self, limit: int = 100) -> list[GoldPrice]:
        """获取最近的金价记录"""
        with self.get_session() as session:
            return session.query(GoldPrice).order_by(GoldPrice.timestamp.desc()).limit(limit).all()

    def get_prices_in_range(self, start: datetime, end: datetime) -> list[GoldPrice]:
        """获取时间范围内的金价"""
        with self.get_session() as session:
            return session.query(GoldPrice).filter(
                GoldPrice.timestamp >= start,
                GoldPrice.timestamp <= end
            ).order_by(GoldPrice.timestamp.asc()).all()

    def save_alert(self, alert_type: str, price: float, message: str) -> AlertRecord:
        """保存告警记录"""
        with self.get_session() as session:
            record = AlertRecord(alert_type=alert_type, price=price, message=message)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    # ============ 告警状态持久化 ============

    def get_alert_state(self, alert_type: str) -> AlertState | None:
        """获取告警状态"""
        with self.get_session() as session:
            return session.query(AlertState).filter(AlertState.alert_type == alert_type).first()

    def save_alert_state(self, alert_type: str, last_triggered_at: datetime | None = None,
                         cooldown_until: datetime | None = None, base_price: float | None = None,
                         window_prices: list[tuple[datetime, float]] | None = None) -> AlertState:
        """保存或更新告警状态"""
        with self.get_session() as session:
            state = session.query(AlertState).filter(AlertState.alert_type == alert_type).first()
            if not state:
                state = AlertState(alert_type=alert_type)
                session.add(state)

            if last_triggered_at is not None:
                state.last_triggered_at = last_triggered_at
            if cooldown_until is not None:
                state.cooldown_until = cooldown_until
            if base_price is not None:
                state.base_price = base_price
            if window_prices is not None:
                state.set_window_prices(window_prices)

            state.updated_at = _utcnow()
            session.commit()
            session.refresh(state)
            return state

    def get_all_alert_states(self) -> list[AlertState]:
        """获取所有告警状态"""
        with self.get_session() as session:
            return session.query(AlertState).all()

    # ============ 通知日志 ============

    def save_notification_log(self, channel: str, status: str, alert_id: int | None = None,
                              error_message: str | None = None, retry_count: int = 0) -> NotificationLog:
        """保存通知发送日志"""
        with self.get_session() as session:
            log = NotificationLog(
                alert_id=alert_id,
                channel=channel,
                status=status,
                error_message=error_message,
                retry_count=retry_count
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            return log

    def get_notification_logs(self, limit: int = 100, channel: str | None = None) -> list[NotificationLog]:
        """获取通知日志"""
        with self.get_session() as session:
            query = session.query(NotificationLog)
            if channel:
                query = query.filter(NotificationLog.channel == channel)
            return query.order_by(NotificationLog.sent_at.desc()).limit(limit).all()

    # ============ 通知配置 ============

    def get_notification_config(self, channel_type: str) -> NotificationConfig | None:
        """获取通知渠道配置"""
        with self.get_session() as session:
            return session.query(NotificationConfig).filter(
                NotificationConfig.channel_type == channel_type
            ).first()

    def save_notification_config(self, channel_type: str, enabled: bool | None = None,
                                  config: dict | None = None) -> NotificationConfig:
        """保存或更新通知渠道配置"""
        with self.get_session() as session:
            nc = session.query(NotificationConfig).filter(
                NotificationConfig.channel_type == channel_type
            ).first()
            if not nc:
                nc = NotificationConfig(channel_type=channel_type)
                session.add(nc)

            if enabled is not None:
                nc.enabled = enabled
            if config is not None:
                nc.set_config(config)

            nc.updated_at = _utcnow()
            session.commit()
            session.refresh(nc)
            return nc

    def get_all_notification_configs(self) -> list[NotificationConfig]:
        """获取所有通知渠道配置"""
        with self.get_session() as session:
            return session.query(NotificationConfig).all()

    # ============ 分析记录 ============

    def save_analysis_record(self, analysis_type: str, model_provider: str | None = None,
                              model_name: str | None = None, price_range_start: datetime | None = None,
                              price_range_end: datetime | None = None, input_summary: str | None = None,
                              result: dict | None = None) -> AnalysisRecord:
        """保存分析记录"""
        with self.get_session() as session:
            record = AnalysisRecord(
                analysis_type=analysis_type,
                model_provider=model_provider,
                model_name=model_name,
                price_range_start=price_range_start,
                price_range_end=price_range_end,
                input_summary=input_summary
            )
            if result:
                record.set_result(result)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_analysis_records(self, limit: int = 50, analysis_type: str | None = None) -> list[AnalysisRecord]:
        """获取分析记录"""
        with self.get_session() as session:
            query = session.query(AnalysisRecord)
            if analysis_type:
                query = query.filter(AnalysisRecord.analysis_type == analysis_type)
            return query.order_by(AnalysisRecord.created_at.desc()).limit(limit).all()

    # ============ 数据生命周期 ============

    def delete_old_prices(self, before: datetime) -> int:
        """删除指定时间之前的价格数据"""
        with self.get_session() as session:
            count = session.query(GoldPrice).filter(GoldPrice.timestamp < before).delete()
            session.commit()
            return count

    def get_price_count(self) -> int:
        """获取价格记录总数"""
        with self.get_session() as session:
            return session.query(GoldPrice).count()

    def get_oldest_price(self) -> GoldPrice | None:
        """获取最早的价格记录"""
        with self.get_session() as session:
            return session.query(GoldPrice).order_by(GoldPrice.timestamp.asc()).first()
