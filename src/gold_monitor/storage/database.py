"""Synchronous repositories with one serialized async execution boundary.

Each operation creates and closes its own session. Async callers use ``run``;
no session is passed between the event loop and the worker thread.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from threading import RLock
from typing import Callable, ParamSpec, TypeVar

from sqlalchemy import Integer, cast, create_engine, func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ..models import (
    AlertRecord,
    AlertState,
    AnalysisRecord,
    Base,
    GoldPrice,
    NotificationConfig,
    NotificationLog,
    _utcnow,
)
from ..time_utils import iso_utc

P = ParamSpec("P")
T = TypeVar("T")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class Database:
    """数据库管理类"""

    def __init__(self, database_url: str):
        url = make_url(database_url)
        options: dict = {}
        if url.get_backend_name() == "sqlite":
            options["connect_args"] = {"check_same_thread": False}
            if url.database in (None, "", ":memory:"):
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, echo=False, **options)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._lock = RLock()
        self._close_lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gold-db")
        self._closed = False
        self._closing = False

    async def run(
        self, operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """Run a complete repository operation off the event loop.

        Cancellation waits for the in-flight transaction before propagating, so
        shutdown never disposes a connection while a worker is committing.
        """
        if self._closed or self._closing:
            raise RuntimeError("数据库已关闭")
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, partial(operation, *args, **kwargs)
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await future
            except Exception:
                pass
            raise

    def close(self) -> None:
        """Drain repository work, then dispose connections. Safe to call twice."""
        with self._close_lock:
            if not self._closed:
                self._closing = True
                self._executor.shutdown(wait=True)
                with self._lock:
                    self.engine.dispose()
                    self._closed = True

    dispose = close

    @property
    def closed(self) -> bool:
        """Whether queued operations and owned connections have been drained."""
        return self._closed

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    @staticmethod
    def _query_limit(limit: int) -> int:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100000
        ):
            raise ValueError("查询条数必须在 1 到 100000 之间")
        return limit

    def create_tables(self):
        """创建所有表"""
        with self._lock:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def get_session(self):
        """Create, serialize and close a session within the calling thread."""
        with self._lock:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            with self.SessionLocal() as session:
                yield session

    def save_price(
        self,
        price: float,
        source: str = "mock",
        *,
        timestamp: datetime | None = None,
        currency: str = "USD",
    ) -> GoldPrice:
        """保存金价记录"""
        with self.get_session() as session:
            record = GoldPrice(
                price=price,
                source=source,
                currency=currency,
                timestamp=_naive_utc(timestamp) if timestamp is not None else _utcnow(),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_latest_price(self, *, currency: str | None = None) -> GoldPrice | None:
        """获取最新金价"""
        with self.get_session() as session:
            query = session.query(GoldPrice)
            if currency is not None:
                query = query.filter(GoldPrice.currency == currency)
            return query.order_by(
                GoldPrice.timestamp.desc(), GoldPrice.id.desc()
            ).first()

    def get_recent_prices(
        self,
        limit: int = 100,
        *,
        currency: str | None = None,
    ) -> list[GoldPrice]:
        """获取最近的金价记录"""
        with self.get_session() as session:
            query = session.query(GoldPrice)
            if currency is not None:
                query = query.filter(GoldPrice.currency == currency)
            return (
                query.order_by(GoldPrice.timestamp.desc(), GoldPrice.id.desc())
                .limit(self._query_limit(limit))
                .all()
            )

    def get_prices_in_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 10000,
        *,
        newest_first: bool = False,
        offset: int = 0,
        currency: str | None = None,
    ) -> list[GoldPrice]:
        """获取时间范围内的金价"""
        start, end = _naive_utc(start), _naive_utc(end)
        if start > end or offset < 0:
            raise ValueError("无效的查询范围")
        order = (
            (GoldPrice.timestamp.desc(), GoldPrice.id.desc())
            if newest_first
            else (GoldPrice.timestamp.asc(), GoldPrice.id.asc())
        )
        with self.get_session() as session:
            query = session.query(GoldPrice).filter(
                GoldPrice.timestamp >= start, GoldPrice.timestamp <= end
            )
            if currency is not None:
                query = query.filter(GoldPrice.currency == currency)
            return (
                query.order_by(*order)
                .offset(offset)
                .limit(self._query_limit(limit))
                .all()
            )

    def get_chart_data(
        self,
        start: datetime,
        end: datetime,
        max_points: int = 2000,
        currency: str = "USD",
    ) -> dict:
        """Summarize the entire window; sample extrema in chronological buckets.

        Window functions assign equally sized groups of observations. Each group
        contributes its minimum and maximum, and the first/last quotes are always
        retained. Only bounded result sets leave the database; statistics use all
        matching observations, never the sampled points.
        """
        self._query_limit(max_points)
        if max_points < 2:
            raise ValueError("图表至少需要保留 2 个点")
        start, end = _naive_utc(start), _naive_utc(end)
        if start > end:
            raise ValueError("无效的查询范围")
        filters = (
            GoldPrice.timestamp >= start,
            GoldPrice.timestamp <= end,
            GoldPrice.currency == currency,
        )
        result = {
            "timestamps": [],
            "prices": [],
            "current_price": 0.0,
            "price_change": 0.0,
            "price_change_percent": 0.0,
            "high": 0.0,
            "low": 0.0,
            "average": 0.0,
            "count": 0,
            "window_start": iso_utc(start),
            "window_end": iso_utc(end),
        }
        with self.get_session() as session:
            count, low, high, average = session.execute(
                select(
                    func.count(GoldPrice.id),
                    func.min(GoldPrice.price),
                    func.max(GoldPrice.price),
                    func.avg(GoldPrice.price),
                ).where(*filters)
            ).one()
            if count == 0:
                return result
            query = session.query(GoldPrice).filter(*filters)
            first = query.order_by(GoldPrice.timestamp, GoldPrice.id).first()
            last = query.order_by(
                GoldPrice.timestamp.desc(), GoldPrice.id.desc()
            ).first()
            assert first is not None and last is not None
            if count <= max_points:
                rows = (
                    query.order_by(GoldPrice.timestamp, GoldPrice.id)
                    .limit(max_points)
                    .all()
                )
                points = [(row.id, row.timestamp, row.price) for row in rows]
            else:
                points = [
                    (first.id, first.timestamp, first.price),
                    (last.id, last.timestamp, last.price),
                ]
                if max_points == 3:
                    # With one remaining slot, keep the strongest departure from the endpoints.
                    extreme = (
                        query.filter(GoldPrice.id.notin_([first.id, last.id]))
                        .order_by(
                            func.abs(
                                GoldPrice.price - (first.price + last.price) / 2
                            ).desc(),
                            GoldPrice.timestamp,
                            GoldPrice.id,
                        )
                        .first()
                    )
                    assert extreme is not None
                    points.append((extreme.id, extreme.timestamp, extreme.price))
                elif max_points >= 4:
                    bucket_count = (max_points - 2) // 2
                    bucket_size = (count - 2 + bucket_count - 1) // bucket_count
                    numbered = (
                        select(
                            GoldPrice.id,
                            GoldPrice.timestamp,
                            GoldPrice.price,
                            func.row_number()
                            .over(order_by=(GoldPrice.timestamp, GoldPrice.id))
                            .label("position"),
                        )
                        .where(*filters)
                        .subquery()
                    )
                    bucket = cast((numbered.c.position - 2) / bucket_size, Integer)
                    ranked = (
                        select(
                            numbered.c.id,
                            numbered.c.timestamp,
                            numbered.c.price,
                            func.row_number()
                            .over(
                                partition_by=bucket,
                                order_by=(
                                    numbered.c.price,
                                    numbered.c.timestamp,
                                    numbered.c.id,
                                ),
                            )
                            .label("low_rank"),
                            func.row_number()
                            .over(
                                partition_by=bucket,
                                order_by=(
                                    numbered.c.price.desc(),
                                    numbered.c.timestamp,
                                    numbered.c.id,
                                ),
                            )
                            .label("high_rank"),
                        )
                        .where(numbered.c.position > 1, numbered.c.position < count)
                        .subquery()
                    )
                    samples = session.execute(
                        select(
                            ranked.c.id,
                            ranked.c.timestamp,
                            ranked.c.price,
                        )
                        .where(or_(ranked.c.low_rank == 1, ranked.c.high_rank == 1))
                        .order_by(ranked.c.timestamp, ranked.c.id)
                        .limit(max_points - 2)
                    )
                    points.extend((row.id, row.timestamp, row.price) for row in samples)
                points.sort(key=lambda point: (point[1], point[0]))
            result.update(
                {
                    "timestamps": [iso_utc(point[1]) for point in points],
                    "prices": [float(point[2]) for point in points],
                    "current_price": float(last.price),
                    "price_change": float(last.price - first.price),
                    "price_change_percent": (last.price - first.price)
                    / first.price
                    * 100
                    if first.price
                    else 0.0,
                    "high": float(high),
                    "low": float(low),
                    "average": float(average),
                    "count": int(count),
                }
            )
            return result

    def save_alert(self, alert_type: str, price: float, message: str) -> AlertRecord:
        """保存告警记录"""
        with self.get_session() as session:
            record = AlertRecord(alert_type=alert_type, price=price, message=message)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    # ============ 告警状态持久化 ============

    def save_alert_with_state(
        self,
        alert_type: str,
        price: float,
        message: str,
        *,
        triggered_at: datetime,
        cooldown_until: datetime,
    ) -> AlertRecord:
        """Commit an alert and its cooldown together, or roll both back."""
        with self.get_session() as session:
            with session.begin():
                record = AlertRecord(
                    alert_type=alert_type,
                    price=price,
                    message=message,
                    triggered_at=_naive_utc(triggered_at),
                )
                session.add(record)
                state = (
                    session.query(AlertState).filter_by(alert_type=alert_type).first()
                )
                if state is None:
                    state = AlertState(alert_type=alert_type)
                    session.add(state)
                state.last_triggered_at = _naive_utc(triggered_at)
                state.cooldown_until = _naive_utc(cooldown_until)
                state.updated_at = _utcnow()
            return record

    def get_alert_history(self, limit: int = 50) -> list[AlertRecord]:
        return self.get_alerts(limit)

    def get_alerts(
        self,
        limit: int = 50,
        alert_type: str | None = None,
        start: datetime | None = None,
    ) -> list[AlertRecord]:
        with self.get_session() as session:
            query = session.query(AlertRecord)
            if alert_type is not None:
                query = query.filter(AlertRecord.alert_type == alert_type)
            if start is not None:
                query = query.filter(AlertRecord.triggered_at >= _naive_utc(start))
            return (
                query.order_by(AlertRecord.triggered_at.desc(), AlertRecord.id.desc())
                .limit(self._query_limit(limit))
                .all()
            )

    def get_alert_state(self, alert_type: str) -> AlertState | None:
        """获取告警状态"""
        with self.get_session() as session:
            return (
                session.query(AlertState)
                .filter(AlertState.alert_type == alert_type)
                .first()
            )

    def save_alert_state(
        self,
        alert_type: str,
        last_triggered_at: datetime | None = None,
        cooldown_until: datetime | None = None,
        base_price: float | None = None,
        window_prices: list[tuple[datetime, float]] | None = None,
    ) -> AlertState:
        """保存或更新告警状态"""
        with self.get_session() as session:
            state = (
                session.query(AlertState)
                .filter(AlertState.alert_type == alert_type)
                .first()
            )
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

    def save_notification_log(
        self,
        channel: str,
        status: str,
        alert_id: int | None = None,
        error_message: str | None = None,
        retry_count: int = 0,
    ) -> NotificationLog:
        """保存通知发送日志"""
        with self.get_session() as session:
            log = NotificationLog(
                alert_id=alert_id,
                channel=channel,
                status=status,
                error_message=error_message,
                retry_count=retry_count,
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            return log

    def get_notification_logs(
        self, limit: int = 100, channel: str | None = None
    ) -> list[NotificationLog]:
        """获取通知日志"""
        with self.get_session() as session:
            query = session.query(NotificationLog)
            if channel:
                query = query.filter(NotificationLog.channel == channel)
            return (
                query.order_by(NotificationLog.sent_at.desc())
                .limit(self._query_limit(limit))
                .all()
            )

    # ============ 通知配置 ============

    def get_notification_config(self, channel_type: str) -> NotificationConfig | None:
        """获取通知渠道配置"""
        with self.get_session() as session:
            return (
                session.query(NotificationConfig)
                .filter(NotificationConfig.channel_type == channel_type)
                .first()
            )

    def save_notification_config(
        self, channel_type: str, enabled: bool | None = None, config: dict | None = None
    ) -> NotificationConfig:
        """保存或更新通知渠道配置"""
        with self.get_session() as session:
            nc = (
                session.query(NotificationConfig)
                .filter(NotificationConfig.channel_type == channel_type)
                .first()
            )
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

    def get_notification_configs(self) -> list[NotificationConfig]:
        return self.get_all_notification_configs()

    # ============ 分析记录 ============

    def save_analysis_record(
        self,
        analysis_type: str,
        model_provider: str | None = None,
        model_name: str | None = None,
        price_range_start: datetime | None = None,
        price_range_end: datetime | None = None,
        input_summary: str | None = None,
        result: dict | None = None,
    ) -> AnalysisRecord:
        """保存分析记录"""
        with self.get_session() as session:
            record = AnalysisRecord(
                analysis_type=analysis_type,
                model_provider=model_provider,
                model_name=model_name,
                price_range_start=price_range_start,
                price_range_end=price_range_end,
                input_summary=input_summary,
            )
            if result:
                record.set_result(result)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_analysis_records(
        self, limit: int = 50, analysis_type: str | None = None
    ) -> list[AnalysisRecord]:
        """获取分析记录"""
        with self.get_session() as session:
            query = session.query(AnalysisRecord)
            if analysis_type:
                query = query.filter(AnalysisRecord.analysis_type == analysis_type)
            return (
                query.order_by(AnalysisRecord.created_at.desc())
                .limit(self._query_limit(limit))
                .all()
            )

    def get_analysis_record(self, record_id: int) -> AnalysisRecord | None:
        with self.get_session() as session:
            return session.get(AnalysisRecord, record_id)

    # ============ 数据生命周期 ============

    def delete_old_prices(self, before: datetime) -> int:
        """删除指定时间之前的价格数据"""
        with self.get_session() as session:
            count = (
                session.query(GoldPrice).filter(GoldPrice.timestamp < before).delete()
            )
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
