"""FastAPI Web 服务 - 纯 Web 模式，自动数据采集 + WebSocket 实时推送"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import __version__
from .config import settings
from .models import Database
from .collector import (
    AdvancedCollector, FetchStrategy,
    create_data_source, set_collector, get_collector
)
from .alert import AlertMonitor, Alert
from .analyzer import GoldAnalyzer
from .llm_config import get_llm_config_manager, ModelProvider
from .data_sources.base import PriceData
from .security import (
    get_api_key_auth, get_rate_limiter, is_admin_path, is_rate_limited_path, APIKeyAuth, RateLimiter
)
from .data_lifecycle import (
    DataLifecycleManager, get_lifecycle_manager, set_lifecycle_manager,
    start_cleanup_scheduler, stop_cleanup_scheduler
)
from .metrics import (
    get_metrics, get_metrics_content_type, record_price, record_alert,
    update_ws_connections, record_ws_message,
    set_system_info, update_db_records, MetricsMiddleware, PROMETHEUS_AVAILABLE
)

logger = logging.getLogger(__name__)

# 首页 HTML（已抽离到 static/index.html，模块加载时读入一次）
_INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

# 全局实例
db = Database(settings.database_url)
db.create_tables()

# 全局状态
_alert_monitor: Optional[AlertMonitor] = None
_alerts_buffer: list[Alert] = []  # 最近的告警缓存

# 智能分析缓存
_smart_analysis_cache: Optional[dict] = None
_smart_analysis_lock = asyncio.Lock()

# 安全组件
_api_auth: Optional[APIKeyAuth] = None
_rate_limiter: Optional[RateLimiter] = None

def get_auth() -> APIKeyAuth:
    """获取 API 鉴权组件"""
    global _api_auth
    if _api_auth is None:
        _api_auth = get_api_key_auth()
    return _api_auth

def get_limiter() -> RateLimiter:
    """获取限流组件"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = get_rate_limiter(settings.rate_limit_per_minute)
    return _rate_limiter


async def require_admin_dep(request: Request):
    """管理员鉴权依赖项 - 独立于安全中间件的纵深防御。

    即使 ADMIN_PATHS 前缀配置遗漏，挂了此依赖的高危端点仍受保护。
    与中间件一致：仅在 enable_auth=True 时强制校验。
    """
    return await get_auth().require_admin(request)


# ============ WebSocket 连接管理器 ============

class ConnectionManager:
    """WebSocket 连接管理器 - 管理所有客户端连接"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """接受新连接"""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info("WebSocket 客户端连接，当前连接数: %d", len(self.active_connections))

    async def disconnect(self, websocket: WebSocket):
        """断开连接"""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info("WebSocket 客户端断开，当前连接数: %d", len(self.active_connections))

    async def broadcast(self, message: dict):
        """广播消息到所有连接"""
        if not self.active_connections:
            return

        data = json.dumps(message, default=str)
        disconnected = []

        async with self._lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

        # 清理断开的连接
        for conn in disconnected:
            await self.disconnect(conn)

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)


# 全局 WebSocket 管理器
ws_manager = ConnectionManager()


async def on_price_update(price_data: PriceData):
    """价格更新回调 - 检查告警 + WebSocket 广播 + 记录指标"""
    global _alerts_buffer

    # 记录 Prometheus 指标
    record_price(price_data.price)
    update_ws_connections(ws_manager.connection_count)

    # 广播价格更新
    await ws_manager.broadcast({
        "type": "price_update",
        "data": {
            "price": price_data.price,
            "currency": price_data.currency,
            "source": price_data.source,
            "timestamp": price_data.timestamp.isoformat()
        }
    })
    record_ws_message("price_update")

    # 检查告警
    if _alert_monitor:
        alerts = await _alert_monitor.check_price(price_data)
        if alerts:
            _alerts_buffer.extend(alerts)
            _alerts_buffer = _alerts_buffer[-100:]

            # 广播告警并记录指标
            for alert in alerts:
                record_alert(alert.alert_type.value)
                await ws_manager.broadcast({
                    "type": "alert",
                    "data": {
                        "alert_type": alert.alert_type.value,
                        "price": alert.price,
                        "message": alert.message,
                        "triggered_at": alert.triggered_at.isoformat()
                    }
                })
                record_ws_message("alert")


# 定时任务：每天0点自动分析
_daily_analysis_task: Optional[asyncio.Task] = None


async def _daily_analysis_scheduler():
    """每天0点执行智能分析的调度器"""
    while True:
        try:
            # 计算距离下一个0点的秒数
            now = datetime.now()
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            seconds_until_midnight = (tomorrow - now).total_seconds()
            
            logger.info(f"智能分析定时任务：将在 {seconds_until_midnight/3600:.1f} 小时后执行（明天0点）")
            
            # 等待到0点
            await asyncio.sleep(seconds_until_midnight)
            
            # 执行分析
            logger.info("定时任务触发：开始执行每日智能分析")
            await _run_smart_analysis()
            logger.info("每日智能分析完成")
            
            # 等待1分钟，避免重复触发
            await asyncio.sleep(60)
            
        except asyncio.CancelledError:
            logger.info("智能分析定时任务已取消")
            break
        except Exception as e:
            logger.error(f"智能分析定时任务出错: {e}")
            # 出错后等待1小时再重试
            await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _alert_monitor, _daily_analysis_task

    logger.info("金价监控系统启动")

    # 初始化告警监控器（带状态持久化）
    _alert_monitor = AlertMonitor(
        database=db,
        channels=[],
        threshold_upper=settings.alert_price_upper,
        threshold_lower=settings.alert_price_lower,
        volatility_percent=settings.alert_threshold_percent,
        volatility_window_minutes=settings.alert_volatility_window,
        use_smart_volatility=True,  # 使用抗噪波动检测
        persist_interval=60  # 每60秒持久化状态
    )

    # 创建并启动高级采集器
    # 根据数据源配置选择策略
    if settings.data_source == "fallback":
        strategy = FetchStrategy.FALLBACK
    elif settings.data_source in ("mock", "sina", "goldapi"):
        strategy = FetchStrategy.SINGLE
    else:
        strategy = FetchStrategy.PARALLEL_FIRST  # 默认并行取最快

    collector = AdvancedCollector(
        database=db,
        strategy=strategy,
        on_price_update=on_price_update,
        deduplicate=True,
        dedupe_threshold=0.01,
        gap_detection=True,
        gap_threshold_minutes=5
    )
    set_collector(collector)
    collector.start(settings.fetch_interval)

    # 初始化数据生命周期管理器
    lifecycle_manager = DataLifecycleManager(db)
    set_lifecycle_manager(lifecycle_manager)
    
    # 启动定时清理任务（每24小时）
    start_cleanup_scheduler(lifecycle_manager, interval_hours=24)
    logger.info("数据生命周期管理器已启动")
    
    # 设置 Prometheus 系统信息
    if PROMETHEUS_AVAILABLE:
        set_system_info(
            version=__version__,
            data_source=settings.data_source,
            fetch_interval=settings.fetch_interval
        )
        logger.info("Prometheus 指标已启用")
    
    # 启动每日智能分析定时任务
    _daily_analysis_task = asyncio.create_task(_daily_analysis_scheduler())
    logger.info("每日智能分析定时任务已启动（每天0点执行）")
    
    # 启动时执行一次智能分析（如果没有缓存）
    if _smart_analysis_cache is None:
        asyncio.create_task(_run_smart_analysis())

    yield

    # 关闭时
    logger.info("金价监控系统关闭")
    
    # 强制持久化告警状态
    if _alert_monitor:
        _alert_monitor.force_persist()
        # 等待后台通知任务完成，避免关停时丢失正在发送的通知
        await _alert_monitor.wait_pending_notifications()

    # 停止定时清理任务
    stop_cleanup_scheduler()
    
    # 取消定时任务
    if _daily_analysis_task:
        _daily_analysis_task.cancel()
        try:
            await _daily_analysis_task
        except asyncio.CancelledError:
            pass
    
    collector = get_collector()
    if collector:
        await collector.stop()


app = FastAPI(
    title="金价实时监控系统",
    description="实时获取金价、告警通知、AI 分析",
    version=__version__,
    lifespan=lifespan
)

# CORS 配置
# 默认仅同源（allow_origins 为空）；内置 Web UI 与 API 同源，无需跨域。
# 仅当显式配置 GOLD_CORS_ALLOW_ORIGINS 时才放行对应来源，并随之允许携带凭证。
# 绝不使用 "*" + allow_credentials=True 这种被规范禁止且不安全的组合。
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus 指标中间件
if PROMETHEUS_AVAILABLE:
    app.add_middleware(MetricsMiddleware)


# ============ 安全中间件 ============

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """安全中间件 - API鉴权和限流"""
    path = request.url.path
    
    # 限流检查（仅对需要限流的路径）
    if is_rate_limited_path(path) and settings.enable_auth:
        limiter = get_limiter()
        allowed, remaining = limiter.is_allowed(request)
        if not allowed:
            return Response(
                content='{"detail": "请求过于频繁，请稍后再试"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"}
            )
    
    # 管理员权限检查（仅对管理路径且启用鉴权时）
    if is_admin_path(path) and settings.enable_auth:
        auth = get_auth()
        if not auth.verify_admin_key(request):
            return Response(
                content='{"detail": "需要管理员权限，请提供有效的 X-Admin-Key 头"}',
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "API-Key"}
            )
    
    response = await call_next(request)
    return response


# ============ WebSocket 端点 ============

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 实时价格推送
    
    消息格式:
    - 价格更新: {"type": "price_update", "data": {...}}
    - 告警通知: {"type": "alert", "data": {...}}
    - 心跳: {"type": "ping"} -> {"type": "pong"}
    """
    await ws_manager.connect(websocket)

    # 连接成功后发送当前价格
    collector = get_collector()
    if collector and collector.last_price:
        await websocket.send_json({
            "type": "price_update",
            "data": {
                "price": collector.last_price.price,
                "currency": collector.last_price.currency,
                "source": collector.last_price.source,
                "timestamp": collector.last_price.timestamp.isoformat()
            }
        })

    try:
        while True:
            # 接收客户端消息（心跳检测）
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)


@app.get("/ws/status")
async def websocket_status():
    """WebSocket 连接状态"""
    return {
        "active_connections": ws_manager.connection_count,
        "endpoint": "/ws"
    }


# ============ 数据模型 ============

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


# ============ API 路由 ============

@app.get("/", response_class=HTMLResponse)
async def root():
    """首页 - 金价走势图仪表盘（HTML 见 static/index.html）"""
    return _INDEX_HTML


# 应用启动时间
_app_start_time = datetime.now(timezone.utc).replace(tzinfo=None)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    last_record = db.get_latest_price()
    collector = get_collector()

    # 检查数据源健康状态
    data_source_healthy = False
    try:
        source = create_data_source()
        data_source_healthy = await source.health_check()
    except Exception:
        data_source_healthy = False

    # 检查数据库连接
    db_status = "connected"
    try:
        from sqlalchemy import text
        with db.get_session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"

    # 计算运行时间
    uptime = (datetime.now(timezone.utc).replace(tzinfo=None) - _app_start_time).total_seconds()

    # 检查采集器状态
    collector_running = collector is not None and collector.is_running
    collector_stats = collector.stats.to_dict() if collector else None

    # 判断整体健康状态
    status = "healthy" if db_status == "connected" and collector_running else "unhealthy"

    return HealthResponse(
        status=status,
        database=db_status,
        data_source=settings.data_source,
        data_source_healthy=data_source_healthy,
        collector_running=collector_running,
        collector_stats=collector_stats,
        last_price=last_record.price if last_record else None,
        last_update=last_record.timestamp if last_record else None,
        uptime_seconds=uptime,
        fetch_interval=settings.fetch_interval
    )


@app.get("/api/chart/data", response_model=ChartDataResponse)
async def get_chart_data(
    hours: int = Query(default=24, ge=1, le=43800, description="查询小时数")
):
    """获取图表数据"""
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)

    if not records:
        # 如果没有数据，尝试获取最新价格
        try:
            source = create_data_source()
            price_data = await source.fetch_price()
            db.save_price(price_data.price, price_data.source)
            records = [db.get_latest_price()]
        except Exception:
            return ChartDataResponse(
                timestamps=[],
                prices=[],
                current_price=0,
                price_change=0,
                price_change_percent=0,
                high=0,
                low=0
            )

    # 根据时间范围调整时间格式
    if hours <= 24:
        time_format = "%H:%M"
    elif hours <= 168:
        time_format = "%m-%d %H:%M"
    else:
        time_format = "%Y-%m-%d"

    timestamps = [r.timestamp.strftime(time_format) for r in records]
    prices = [r.price for r in records]

    current_price = prices[-1] if prices else 0
    first_price = prices[0] if prices else 0
    price_change = current_price - first_price
    price_change_percent = (price_change / first_price * 100) if first_price > 0 else 0

    return ChartDataResponse(
        timestamps=timestamps,
        prices=prices,
        current_price=current_price,
        price_change=price_change,
        price_change_percent=price_change_percent,
        high=max(prices) if prices else 0,
        low=min(prices) if prices else 0
    )


@app.get("/api/price/current", response_model=PriceResponse)
async def get_current_price():
    """获取当前金价"""
    source = create_data_source()
    price_data = await source.fetch_price()

    # 保存到数据库
    db.save_price(price_data.price, price_data.source)

    return PriceResponse(
        price=price_data.price,
        source=price_data.source,
        timestamp=price_data.timestamp
    )


@app.get("/api/price/latest", response_model=PriceResponse)
async def get_latest_price():
    """获取最新存储的金价（不请求数据源）"""
    record = db.get_latest_price()
    if not record:
        raise HTTPException(status_code=404, detail="没有价格数据")

    return PriceResponse(
        price=record.price,
        source=record.source,
        timestamp=record.timestamp
    )


@app.get("/api/price/history", response_model=PriceHistoryResponse)
async def get_price_history(
    hours: int = Query(default=24, ge=1, le=168, description="查询小时数"),
    limit: int = Query(default=100, ge=1, le=1000, description="最大记录数")
):
    """获取历史价格"""
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)
    if limit and len(records) > limit:
        records = records[-limit:]

    prices = [r.price for r in records] if records else []
    stats = {
        "max": max(prices) if prices else 0,
        "min": min(prices) if prices else 0,
        "avg": sum(prices) / len(prices) if prices else 0,
        "count": len(prices)
    }

    return PriceHistoryResponse(
        data=[
            PriceResponse(
                price=r.price,
                source=r.source,
                timestamp=r.timestamp
            ) for r in records
        ],
        count=len(records),
        stats=stats
    )


@app.get("/api/alerts", response_model=list[AlertResponse])
async def get_alerts(
    limit: int = Query(default=50, ge=1, le=200, description="最大记录数"),
    alert_type: Optional[str] = Query(default=None, description="告警类型过滤: threshold_upper, threshold_lower, volatility"),
    hours: Optional[int] = Query(default=None, ge=1, le=720, description="时间范围（小时）")
):
    """获取告警历史（支持按类型和时间过滤）"""
    from .models import AlertRecord
    with db.get_session() as session:
        query = session.query(AlertRecord)

        # 按类型过滤
        if alert_type:
            query = query.filter(AlertRecord.alert_type == alert_type)

        # 按时间范围过滤
        if hours:
            start_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
            query = query.filter(AlertRecord.triggered_at >= start_time)

        records = query.order_by(
            AlertRecord.triggered_at.desc()
        ).limit(limit).all()

        return [
            AlertResponse(
                id=r.id,
                alert_type=r.alert_type,
                price=r.price,
                message=r.message,
                triggered_at=r.triggered_at
            ) for r in records
        ]


@app.get("/api/collector/stats")
async def get_collector_stats():
    """获取采集器详细统计"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    return {
        "running": collector.is_running,
        "last_price": {
            "price": collector.last_price.price if collector.last_price else None,
            "source": collector.last_price.source if collector.last_price else None,
            "timestamp": collector.last_price.timestamp.isoformat() if collector.last_price else None
        },
        "stats": collector.stats.to_dict(),
        "config": collector.get_config()
    }


@app.get("/api/collector/config")
async def get_collector_config():
    """获取采集器当前配置"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")
    
    return collector.get_config()


@app.post("/api/collector/config")
async def update_collector_config(
    interval: int = Query(None, ge=1, le=3600, description="采集间隔（秒）"),
    strategy: str = Query(None, description="采集策略: single, fallback, parallel_first, parallel_vote")
):
    """运行时修改采集器配置"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")
    
    changes = {}
    
    if interval is not None:
        collector.set_interval(interval)
        changes["interval"] = interval
    
    if strategy is not None:
        try:
            new_strategy = FetchStrategy(strategy)
            collector.set_strategy(new_strategy)
            changes["strategy"] = strategy
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的策略: {strategy}，可选: single, fallback, parallel_first, parallel_vote"
            )
    
    if not changes:
        raise HTTPException(status_code=400, detail="请提供至少一个配置项")
    
    # 广播配置变更
    await ws_manager.broadcast({
        "type": "config_update",
        "data": changes
    })
    
    return {
        "message": "配置已更新",
        "changes": changes,
        "current_config": collector.get_config()
    }


@app.post("/api/collector/fill-gaps")
async def fill_data_gaps(hours: int = Query(24, ge=1, le=168)):
    """检测数据间隙并采集当前样本以接续序列

    注意：数据源仅提供当前现货价，无法回填历史时刻的真实价格，
    因此本接口不会伪造历史数据点，只在存在间隙时采集一个当前样本。
    """
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    # fill_gaps 内部会检测间隙；这里不重复检测以免重复累加统计
    recorded = await collector.fill_gaps(hours)

    return {
        "message": (
            "现货数据源无法回填历史价格，"
            f"已采集 {recorded} 个当前样本以接续序列（如需查看间隙详情见 /api/collector/gaps）"
        ),
        "samples_recorded": recorded,
        "backfill_supported": False,
    }


@app.get("/api/collector/gaps")
async def detect_data_gaps(hours: int = Query(24, ge=1, le=168)):
    """检测数据间隙"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")
    
    gaps = await collector.detect_gaps(hours)
    
    return {
        "gaps": [
            {
                "start": gap[0].isoformat(),
                "end": gap[1].isoformat(),
                "duration_minutes": (gap[1] - gap[0]).total_seconds() / 60
            }
            for gap in gaps
        ],
        "count": len(gaps)
    }


@app.get("/api/analysis", response_model=AnalysisResponse)
async def run_analysis():
    """运行 AI 分析（基于本地数据）"""
    # 优先使用“波动窗口”内的数据，避免窗口描述与实际取数跨度不一致
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(minutes=settings.alert_volatility_window)
    window_records = db.get_prices_in_range(start_time, end_time)

    if len(window_records) >= 2:
        records = window_records  # 时间升序
        time_window_minutes = settings.alert_volatility_window
    else:
        # 回退：使用最近 N 条数据，但用真实时间跨度作为窗口描述
        recent_desc = db.get_recent_prices(limit=20)  # 时间降序
        if len(recent_desc) < 2:
            raise HTTPException(status_code=400, detail="数据不足，无法分析")

        records = list(reversed(recent_desc))  # 时间升序
        span_seconds = (records[-1].timestamp - records[0].timestamp).total_seconds()
        time_window_minutes = max(1, int(span_seconds / 60))

    current_price = records[-1].price
    oldest_price = records[0].price
    price_change = current_price - oldest_price

    recent_prices = [(r.timestamp, r.price) for r in records]

    analyzer = GoldAnalyzer()
    report = await analyzer.analyze_volatility(
        current_price=current_price,
        price_change=price_change,
        recent_prices=recent_prices,
        time_window_minutes=time_window_minutes
    )

    return AnalysisResponse(
        summary=report.summary,
        possible_reasons=report.possible_reasons,
        market_sentiment=report.market_sentiment,
        recommendation=report.recommendation,
        generated_at=report.generated_at
    )


# ============ 智能分析 API（AI 搜索网络数据） ============

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


async def _run_smart_analysis(model: Optional[str] = None) -> dict:
    """执行智能分析并缓存结果"""
    global _smart_analysis_cache
    
    async with _smart_analysis_lock:
        logger.info(f"开始执行智能分析... 模型: {model or '配置默认'}")
        try:
            # 获取配置
            manager = get_llm_config_manager()
            config = manager.reload_config()
            active_provider = config.get_active_provider()
            
            # 确定使用的模型
            use_model = model or config.active_model
            
            # 如果没有指定模型，尝试从 provider 的缓存模型列表中获取
            if not use_model and active_provider and active_provider.models:
                use_model = active_provider.models[0]
                logger.info(f"使用 provider 缓存的第一个模型: {use_model}")
            
            # 如果仍然没有模型，根据 provider 名称推断默认模型
            if not use_model and active_provider:
                provider_name = (active_provider.name or "").lower()
                if "deepseek" in provider_name:
                    use_model = "deepseek-chat"
                elif "qwen" in provider_name or "通义" in provider_name or "dashscope" in (active_provider.base_url or "").lower():
                    use_model = "qwen-turbo"
                elif "moonshot" in provider_name or "kimi" in provider_name:
                    use_model = "moonshot-v1-8k"
                elif "zhipu" in provider_name or "glm" in provider_name:
                    use_model = "glm-4"
                # 其他情况保持 None，让 provider 使用其默认值
            
            if not active_provider or active_provider.id == "mock" or not active_provider.api_key:
                # Mock 模式
                from .analyzer import MockLLMProvider
                provider = MockLLMProvider()
                report = await provider.smart_analyze()
                model_used = "Mock"
            else:
                # 真实 API 调用
                from .analyzer import OpenAIProvider, AnthropicProvider
                
                if "anthropic" in (active_provider.base_url or "").lower() or "claude" in (active_provider.name or "").lower():
                    provider = AnthropicProvider(
                        api_key=active_provider.api_key,
                        model=use_model
                    )
                else:
                    provider = OpenAIProvider(
                        api_key=active_provider.api_key,
                        base_url=active_provider.base_url,
                        model=use_model
                    )
                
                report = await provider.smart_analyze()
                model_used = use_model or provider.model
            
            _smart_analysis_cache = {
                "title": report.title,
                "market_overview": report.market_overview,
                "recent_trend": report.recent_trend,
                "key_factors": report.key_factors,
                "price_prediction": report.price_prediction,
                "buy_timing": report.buy_timing,
                "recommendation": report.recommendation,
                "risk_warning": report.risk_warning,
                "generated_at": report.generated_at,
                "raw_response": report.raw_response,
                "model_used": model_used,
                "web_search_used": getattr(report, "web_search_used", False),
                "sources": getattr(report, "sources", []),
            }
            logger.info(f"智能分析完成，使用模型: {model_used}")
            return _smart_analysis_cache
        except Exception as e:
            logger.error(f"智能分析失败: {e}")
            raise


@app.get("/api/smart-analysis", response_model=SmartAnalysisResponse)
async def get_smart_analysis():
    """获取智能分析结果（使用缓存）"""
    global _smart_analysis_cache
    
    if _smart_analysis_cache:
        # 计算缓存年龄
        cache_age = datetime.now(timezone.utc).replace(tzinfo=None) - _smart_analysis_cache["generated_at"]
        cache_age_minutes = int(cache_age.total_seconds() / 60)
        
        return SmartAnalysisResponse(
            **_smart_analysis_cache,
            is_cached=True,
            cache_age_minutes=cache_age_minutes
        )
    
    # 没有缓存，执行分析
    result = await _run_smart_analysis()
    return SmartAnalysisResponse(
        **result,
        is_cached=False,
        cache_age_minutes=0
    )


@app.post("/api/smart-analysis/refresh")
async def refresh_smart_analysis(request: Optional[RefreshAnalysisRequest] = None):
    """手动刷新智能分析"""
    model = request.model if request else None
    try:
        result = await _run_smart_analysis(model=model)
        return {
            "success": True,
            "message": "分析已刷新",
            "data": result  # 包含完整数据，包括 raw_response
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")


@app.get("/api/config")
async def get_config():
    """获取当前配置（脱敏）"""
    llm_config = get_llm_config_manager().get_config()
    active_provider = llm_config.get_active_provider()
    return {
        "data_source": settings.data_source,
        "fetch_interval": settings.fetch_interval,
        "alert_threshold_percent": settings.alert_threshold_percent,
        "alert_price_upper": settings.alert_price_upper,
        "alert_price_lower": settings.alert_price_lower,
        # 返回当前激活的平台 ID（不返回 API Key）
        "llm_provider": active_provider.id if active_provider else (llm_config.active_provider_id or "mock"),
        "llm_model": llm_config.active_model or None
    }


# ============ LLM 配置 API（多平台支持） ============

@app.get("/api/llm/config")
async def get_llm_config():
    """获取 LLM 配置（API Key 脱敏）"""
    manager = get_llm_config_manager()
    config = manager.reload_config()  # 强制重新加载
    return config.to_safe_dict()


@app.get("/api/llm/status")
async def get_llm_status():
    """获取当前 AI 分析使用的状态"""
    manager = get_llm_config_manager()
    config = manager.reload_config()
    active_provider = config.get_active_provider()
    
    if not active_provider:
        return {
            "enabled": False,
            "mode": "mock",
            "message": "未配置，使用模拟分析",
            "provider_name": None,
            "model": None
        }
    
    if active_provider.id == "mock":
        return {
            "enabled": False,
            "mode": "mock",
            "message": "Mock 模式，返回固定分析结果",
            "provider_name": "Mock",
            "model": None
        }
    
    if not active_provider.api_key:
        return {
            "enabled": False,
            "mode": "mock",
            "message": f"平台 {active_provider.name} 未配置 API Key，使用模拟分析",
            "provider_name": active_provider.name,
            "model": None
        }
    
    return {
        "enabled": True,
        "mode": "ai",
        "message": "已启用 AI 分析",
        "provider_name": active_provider.name,
        "model": config.active_model or "(默认模型)"
    }


@app.get("/api/llm/providers")
async def get_providers():
    """获取所有模型服务平台"""
    manager = get_llm_config_manager()
    config = manager.get_config()
    return {
        "providers": [
            p.to_safe_dict() if isinstance(p, ModelProvider) else ModelProvider(**p).to_safe_dict()
            for p in config.providers
        ],
        "active_provider_id": config.active_provider_id,
        "active_model": config.active_model
    }


@app.post("/api/llm/providers")
async def add_provider(request: ProviderRequest):
    """添加新的模型服务平台"""
    manager = get_llm_config_manager()
    provider = manager.add_provider(
        name=request.name,
        base_url=request.base_url,
        api_key=request.api_key or ""
    )
    return {"success": True, "provider": provider.to_safe_dict()}


@app.put("/api/llm/providers/{provider_id}")
async def update_provider(provider_id: str, request: ProviderUpdateRequest):
    """更新平台配置"""
    manager = get_llm_config_manager()
    
    # 获取当前平台配置
    current = manager.get_provider(provider_id)
    if not current:
        raise HTTPException(status_code=404, detail="平台不存在")
    
    # 如果 api_key 是脱敏格式或为空，保留原有的
    new_api_key = request.api_key
    if not new_api_key or "..." in new_api_key:
        new_api_key = current.api_key
    
    provider = manager.update_provider(
        provider_id,
        name=request.name,
        base_url=request.base_url,
        api_key=new_api_key
    )
    
    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")
    
    return {"success": True, "provider": provider.to_safe_dict()}


@app.delete("/api/llm/providers/{provider_id}")
async def delete_provider(provider_id: str):
    """删除平台"""
    # 不允许删除 mock
    if provider_id == "mock":
        raise HTTPException(status_code=400, detail="不能删除默认的 Mock 平台")
    
    manager = get_llm_config_manager()
    success = manager.delete_provider(provider_id)
    if not success:
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "平台已删除"}


@app.post("/api/llm/active")
async def set_active_provider(request: SetActiveRequest):
    """设置当前使用的平台和模型"""
    manager = get_llm_config_manager()
    success = manager.set_active(request.provider_id, request.model or "")
    if not success:
        raise HTTPException(status_code=404, detail="平台不存在")
    return {"success": True, "message": "已切换"}


@app.post("/api/llm/providers/{provider_id}/models")
async def fetch_provider_models(provider_id: str):
    """获取指定平台的模型列表"""
    import httpx
    
    manager = get_llm_config_manager()
    provider = manager.get_provider(provider_id)
    
    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")
    
    if provider_id == "mock":
        return {"success": True, "models": [], "count": 0, "message": "Mock 模式无模型"}
    
    if not provider.api_key:
        raise HTTPException(status_code=400, detail="请先配置 API Key")
    
    if not provider.base_url:
        raise HTTPException(status_code=400, detail="请先配置 API 地址")
    
    # 智能拼接 URL
    url = provider.base_url.rstrip('/')
    if not url.endswith('/v1') and '/v1' not in url:
        models_url = f"{url}/v1/models"
    else:
        models_url = f"{url}/models"
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {provider.api_key}"}
            )
            
            if resp.status_code == 401:
                raise HTTPException(status_code=401, detail="API Key 无效")
            
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"获取模型失败: {resp.text[:200]}"
                )
            
            data = resp.json()
            models = data.get("data", [])
            
            # 提取模型 ID 并排序
            model_list = []
            for m in models:
                model_id = m.get("id", "")
                if model_id:
                    model_list.append({
                        "id": model_id,
                        "owned_by": m.get("owned_by", ""),
                        "created": m.get("created", 0)
                    })
            
            # 排序
            def sort_key(m):
                id_lower = m["id"].lower()
                priority = 10
                if "gpt-4" in id_lower:
                    priority = 1
                elif "gpt-3.5" in id_lower:
                    priority = 2
                elif "chat" in id_lower:
                    priority = 3
                elif "turbo" in id_lower:
                    priority = 4
                return (priority, m["id"])
            
            model_list.sort(key=sort_key)
            
            # 缓存模型列表
            manager.update_provider_models(provider_id, [m["id"] for m in model_list])
            
            return {
                "success": True,
                "models": model_list,
                "count": len(model_list)
            }
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="请求超时")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"网络错误: {str(e)}")


class TestConnectionRequest(BaseModel):
    """测试连接请求"""
    model: Optional[str] = None


@app.post("/api/llm/providers/{provider_id}/test")
async def test_provider_connection(provider_id: str, request: Optional[TestConnectionRequest] = None):
    """测试指定平台的连接 - 简单发送 hi 测试"""
    manager = get_llm_config_manager()
    provider = manager.get_provider(provider_id)
    
    if not provider:
        raise HTTPException(status_code=404, detail="平台不存在")
    
    # 获取要测试的模型
    test_model = request.model if request and request.model else None
    
    # 如果没有指定模型，尝试从缓存的模型列表或根据名称推断
    if not test_model:
        if provider.models:
            test_model = provider.models[0]
        else:
            provider_name = (provider.name or "").lower()
            if "deepseek" in provider_name:
                test_model = "deepseek-chat"
            elif "qwen" in provider_name or "通义" in provider_name:
                test_model = "qwen-turbo"
            elif "moonshot" in provider_name or "kimi" in provider_name:
                test_model = "moonshot-v1-8k"
            elif "zhipu" in provider_name or "glm" in provider_name:
                test_model = "glm-4"
            elif "openai" in provider_name:
                test_model = "gpt-4o-mini"
            # 如果都不匹配，保持 None 让下面的代码处理
    
    result = {
        "provider_id": provider_id,
        "provider_name": provider.name,
        "model": test_model,
        "success": False,
        "message": "",
        "response": None
    }
    
    if provider_id == "mock":
        result["success"] = True
        result["message"] = "Mock 模式无需连接测试"
        return result
    
    if not provider.api_key:
        result["message"] = "未配置 API Key"
        return result
    
    if not provider.base_url:
        result["message"] = "未配置 API 地址"
        return result
    
    if not test_model:
        result["message"] = "请先获取模型列表或手动指定测试模型"
        return result
    
    try:
        from openai import AsyncOpenAI
        from .analyzer import OpenAIProvider
        
        # 标准化 URL
        base_url = OpenAIProvider._normalize_base_url(provider.base_url)
        
        # 创建客户端
        client = AsyncOpenAI(api_key=provider.api_key, base_url=base_url)
        
        # 简单发送 "hi" 测试连接
        response = await client.chat.completions.create(
            model=test_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50
        )
        
        reply = response.choices[0].message.content
        
        result["success"] = True
        result["message"] = f"连接成功 (模型: {test_model})"
        result["response"] = reply[:100] if reply else "OK"
        
    except Exception as e:
        result["message"] = f"连接失败: {str(e)}"
    
    return result


@app.delete("/api/llm/config")
async def reset_llm_config(_admin: bool = Depends(require_admin_dep)):
    """重置 LLM 配置（需要管理员权限）"""
    manager = get_llm_config_manager()
    success = manager.reset_config()
    
    if not success:
        raise HTTPException(status_code=500, detail="重置配置失败")
    
    return {"message": "配置已重置", "success": True}


# 汇率缓存（简单内存缓存，避免频繁请求）
_exchange_rate_cache = {"usd_cny": 7.2, "updated_at": None}


@app.get("/api/exchange-rate", response_model=ExchangeRateResponse)
async def get_exchange_rate():
    """获取 USD/CNY 汇率"""
    import httpx
    from datetime import timedelta

    # 检查缓存是否有效（30分钟）
    if (_exchange_rate_cache["updated_at"] and
            datetime.now(timezone.utc).replace(tzinfo=None) - _exchange_rate_cache["updated_at"] < timedelta(minutes=30)):
        return ExchangeRateResponse(
            usd_cny=_exchange_rate_cache["usd_cny"],
            updated_at=_exchange_rate_cache["updated_at"]
        )

    # 尝试从免费 API 获取汇率
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 使用央行汇率接口或其他免费接口
            resp = await client.get(
                "https://api.exchangerate-api.com/v4/latest/USD"
            )
            if resp.status_code == 200:
                data = resp.json()
                rate = data.get("rates", {}).get("CNY", 7.2)
                _exchange_rate_cache["usd_cny"] = rate
                _exchange_rate_cache["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception:
        # 获取失败使用缓存或默认值
        if _exchange_rate_cache["updated_at"] is None:
            _exchange_rate_cache["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)

    return ExchangeRateResponse(
        usd_cny=_exchange_rate_cache["usd_cny"],
        updated_at=_exchange_rate_cache["updated_at"]
    )


@app.get("/api/bank-prices", response_model=BankPricesResponse)
async def get_bank_prices():
    """获取各银行金价"""
    from .data_sources.bank import get_bank_source

    bank_source = get_bank_source()
    bank_prices = await bank_source.fetch_all_bank_prices()
    base_price = await bank_source.fetch_base_price_cny()

    # 计算伦敦金人民币价格（作为基准）
    london_gold_cny = base_price

    return BankPricesResponse(
        data=[
            BankPriceResponse(
                bank_name=p.bank_name,
                bank_code=p.bank_code,
                buy_price=p.buy_price,
                sell_price=p.sell_price,
                timestamp=p.timestamp,
                product_name=p.product_name
            ) for p in bank_prices
        ],
        base_price_cny=base_price,
        london_gold_cny=london_gold_cny,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None)
    )


@app.get("/api/convert")
async def convert_gold_price(
    price: float = Query(..., description="价格"),
    from_unit: str = Query(default="oz", description="原单位: oz, g, kg"),
    to_unit: str = Query(default="g", description="目标单位: oz, g, kg"),
    from_currency: str = Query(default="USD", description="原币种: USD, CNY"),
    to_currency: str = Query(default="CNY", description="目标币种: USD, CNY")
):
    """金价单位和币种换算"""
    # 单位换算系数（相对于盎司）
    unit_factors = {
        "oz": 1.0,
        "g": 31.1035,
        "kg": 0.0311035
    }

    # 获取汇率
    usd_cny = _exchange_rate_cache.get("usd_cny", 7.2)
    currency_rates = {
        ("USD", "CNY"): usd_cny,
        ("CNY", "USD"): 1 / usd_cny,
        ("USD", "USD"): 1.0,
        ("CNY", "CNY"): 1.0
    }

    # 先转换单位
    from_factor = unit_factors.get(from_unit, 1.0)
    to_factor = unit_factors.get(to_unit, 1.0)
    converted_price = price * from_factor / to_factor

    # 再转换币种
    rate = currency_rates.get((from_currency, to_currency), 1.0)
    converted_price *= rate

    return {
        "original": {
            "price": price,
            "unit": from_unit,
            "currency": from_currency
        },
        "converted": {
            "price": round(converted_price, 2),
            "unit": to_unit,
            "currency": to_currency
        },
        "exchange_rate": usd_cny
    }


# ============ Prometheus Metrics 端点 ============

@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标端点"""
    # 更新数据库记录数
    try:
        count = db.get_price_count()
        update_db_records(count)
    except Exception:
        pass
    
    return Response(
        content=get_metrics(),
        media_type=get_metrics_content_type()
    )


# ============ 通知配置 API ============

class NotificationConfigRequest(BaseModel):
    """通知渠道配置请求"""
    enabled: Optional[bool] = None
    config: Optional[dict] = None


class NotificationTestRequest(BaseModel):
    """通知测试请求"""
    test_message: Optional[str] = None


@app.get("/api/notifications/config")
async def get_notification_configs():
    """获取所有通知渠道配置"""
    configs = db.get_all_notification_configs()
    return {
        "configs": [
            {
                "channel_type": c.channel_type,
                "enabled": c.enabled,
                "config": c.get_config(),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None
            }
            for c in configs
        ]
    }


@app.get("/api/notifications/config/{channel}")
async def get_notification_config(channel: str):
    """获取指定通知渠道配置"""
    config = db.get_notification_config(channel)
    if not config:
        return {
            "channel_type": channel,
            "enabled": False,
            "config": {},
            "message": "渠道未配置"
        }
    return {
        "channel_type": config.channel_type,
        "enabled": config.enabled,
        "config": config.get_config(),
        "updated_at": config.updated_at.isoformat() if config.updated_at else None
    }


@app.put("/api/notifications/config/{channel}")
async def update_notification_config(channel: str, request: NotificationConfigRequest):
    """更新通知渠道配置"""
    config = db.save_notification_config(
        channel_type=channel,
        enabled=request.enabled,
        config=request.config
    )
    return {
        "success": True,
        "channel_type": config.channel_type,
        "enabled": config.enabled,
        "config": config.get_config()
    }


@app.get("/api/notifications/logs")
async def get_notification_logs(
    limit: int = Query(default=100, le=500),
    channel: Optional[str] = None
):
    """获取通知发送日志"""
    logs = db.get_notification_logs(limit=limit, channel=channel)
    return {
        "logs": [
            {
                "id": log.id,
                "alert_id": log.alert_id,
                "channel": log.channel,
                "status": log.status,
                "error_message": log.error_message,
                "retry_count": log.retry_count,
                "sent_at": log.sent_at.isoformat() if log.sent_at else None
            }
            for log in logs
        ],
        "count": len(logs)
    }


@app.post("/api/notifications/test/{channel}")
async def test_notification(channel: str, request: Optional[NotificationTestRequest] = None):
    """测试通知发送"""
    from .alert import (
        Alert, AlertType, ConsoleNotification,
        EmailNotification, WebhookNotification, TelegramNotification
    )
    
    # 创建测试告警
    test_alert = Alert(
        alert_type=AlertType.VOLATILITY,
        price=2000.0,
        message=request.test_message if request else "这是一条测试通知",
        triggered_at=datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    result = {
        "channel": channel,
        "success": False,
        "message": ""
    }
    
    try:
        if channel == "console":
            notification = ConsoleNotification()
            success = await notification.send(test_alert)
            result["success"] = success
            result["message"] = "控制台通知已发送（查看服务器日志）"
        
        elif channel == "email":
            if not settings.smtp_host or not settings.smtp_username:
                result["message"] = "邮件未配置（SMTP_HOST, SMTP_USERNAME）"
            else:
                notification = EmailNotification(
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    to_addrs=settings.alert_email_to.split(",") if settings.alert_email_to else []
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = "邮件发送成功" if success else "邮件发送失败"
        
        elif channel == "webhook":
            if not settings.webhook_url:
                result["message"] = "Webhook 未配置"
            else:
                notification = WebhookNotification(
                    webhook_url=settings.webhook_url,
                    webhook_type=settings.webhook_type
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = "Webhook 发送成功" if success else "Webhook 发送失败"
        
        elif channel == "telegram":
            if not settings.telegram_bot_token or not settings.telegram_chat_id:
                result["message"] = "Telegram 未配置（BOT_TOKEN, CHAT_ID）"
            else:
                notification = TelegramNotification(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id
                )
                success = await notification.send(test_alert)
                result["success"] = success
                result["message"] = "Telegram 发送成功" if success else "Telegram 发送失败"
        
        else:
            result["message"] = f"不支持的通知渠道: {channel}"
    
    except Exception as e:
        result["message"] = f"发送失败: {str(e)}"
    
    # 记录日志
    db.save_notification_log(
        channel=channel,
        status="success" if result["success"] else "failed",
        error_message=result["message"] if not result["success"] else None
    )
    
    return result


# ============ 数据生命周期 API ============

@app.get("/api/data/stats")
async def get_data_stats():
    """获取数据统计信息"""
    manager = get_lifecycle_manager(db)
    if not manager:
        return {"error": "生命周期管理器未初始化"}
    return await manager.get_stats()


@app.get("/api/data/export")
async def export_data(
    format: str = Query(default="json", description="导出格式: json, csv"),
    start: Optional[str] = Query(default=None, description="起始时间 (ISO格式)"),
    end: Optional[str] = Query(default=None, description="结束时间 (ISO格式)"),
    limit: int = Query(default=1000, le=10000, description="最大记录数")
):
    """导出价格数据"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")
    
    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    
    if format.lower() == "csv":
        content = await manager.export_csv(start=start_dt, end=end_dt, limit=limit)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=gold_prices.csv"}
        )
    else:
        content = await manager.export_json(start=start_dt, end=end_dt, limit=limit)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=gold_prices.json"}
        )


@app.post("/api/data/cleanup")
async def cleanup_data(_admin: bool = Depends(require_admin_dep)):
    """执行数据清理（需要管理员权限）"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")
    
    result = await manager.cleanup()
    return result


@app.post("/api/data/backup")
async def backup_data(backup_name: Optional[str] = None, _admin: bool = Depends(require_admin_dep)):
    """创建数据库备份（需要管理员权限）"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")
    
    result = await manager.backup_database(backup_name)
    return result


@app.get("/api/data/backups")
async def list_backups():
    """列出所有备份文件"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")
    
    backups = await manager.list_backups()
    return {"backups": backups, "count": len(backups)}


# ============ 分析历史 API ============

@app.get("/api/analysis/history")
async def get_analysis_history(
    limit: int = Query(default=50, le=200),
    analysis_type: Optional[str] = Query(default=None, description="分析类型: volatility, smart")
):
    """获取分析历史记录"""
    records = db.get_analysis_records(limit=limit, analysis_type=analysis_type)
    return {
        "records": [
            {
                "id": r.id,
                "analysis_type": r.analysis_type,
                "model_provider": r.model_provider,
                "model_name": r.model_name,
                "price_range_start": r.price_range_start.isoformat() if r.price_range_start else None,
                "price_range_end": r.price_range_end.isoformat() if r.price_range_end else None,
                "input_summary": r.input_summary,
                "result": r.get_result(),
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in records
        ],
        "count": len(records)
    }


@app.get("/api/analysis/history/{record_id}")
async def get_analysis_record(record_id: int):
    """获取单条分析记录详情"""
    with db.get_session() as session:
        from .models import AnalysisRecord
        record = session.query(AnalysisRecord).filter(AnalysisRecord.id == record_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {
            "id": record.id,
            "analysis_type": record.analysis_type,
            "model_provider": record.model_provider,
            "model_name": record.model_name,
            "price_range_start": record.price_range_start.isoformat() if record.price_range_start else None,
            "price_range_end": record.price_range_end.isoformat() if record.price_range_end else None,
            "input_summary": record.input_summary,
            "result": record.get_result(),
            "created_at": record.created_at.isoformat() if record.created_at else None
        }


# ============ 数据源质量评分 API ============

@app.get("/api/source-quality")
async def get_source_quality():
    """获取数据源质量评分"""
    collector = get_collector()
    if not collector:
        return {"error": "采集器未运行", "qualities": [], "confidence": None}
    
    qualities = collector.get_source_quality()
    confidence = collector.get_overall_confidence()
    
    return {
        "qualities": [q.to_dict() for q in qualities],
        "confidence": confidence,
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    }


@app.get("/api/source-quality/confidence")
async def get_data_confidence():
    """获取整体数据置信度（轻量级接口，用于前端实时展示）"""
    collector = get_collector()
    if not collector:
        return {
            "confidence": 0,
            "status": "unknown",
            "message": "采集器未运行"
        }
    
    return collector.get_overall_confidence()


# ============ 安全 API ============

@app.get("/api/security/status")
async def get_security_status():
    """获取安全配置状态"""
    auth = get_auth()
    return {
        "auth_enabled": settings.enable_auth,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "admin_key_configured": bool(settings.admin_api_key),
        "admin_key_hint": auth.admin_key[:8] + "..." if auth.admin_key else None
    }


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """启动 Web 服务"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
