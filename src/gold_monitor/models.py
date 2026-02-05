"""数据库模型定义"""

from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, Float, DateTime, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class GoldPrice(Base):
    """金价记录表"""
    __tablename__ = "gold_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    price = Column(Float, nullable=False, comment="金价 (USD/oz)")
    currency = Column(String(10), default="USD", comment="货币单位")
    source = Column(String(50), comment="数据来源")
    timestamp = Column(DateTime, default=datetime.utcnow, comment="采集时间")

    def __repr__(self):
        return f"<GoldPrice(id={self.id}, price={self.price}, timestamp={self.timestamp})>"


class AlertRecord(Base):
    """告警记录表"""
    __tablename__ = "alert_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(50), nullable=False, comment="告警类型: threshold/volatility")
    price = Column(Float, nullable=False, comment="触发时价格")
    message = Column(Text, comment="告警消息")
    triggered_at = Column(DateTime, default=datetime.utcnow, comment="触发时间")

    def __repr__(self):
        return f"<AlertRecord(id={self.id}, type={self.alert_type}, price={self.price})>"


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
