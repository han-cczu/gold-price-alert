"""FastAPI Web 服务 - 纯 Web 模式，自动数据采集 + WebSocket 实时推送"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .config import settings
from .models import Database
from .collector import (
    AdvancedCollector, PriceCollector, FetchStrategy,
    create_data_source, set_collector, get_collector
)
from .alert import AlertMonitor, Alert
from .analyzer import GoldAnalyzer, SmartAnalysisReport
from .llm_config import get_llm_config_manager, LLMConfig, ModelProvider
from .data_sources.base import PriceData
from .security import (
    get_api_key_auth, get_rate_limiter, get_secret_manager,
    is_admin_path, is_rate_limited_path, APIKeyAuth, RateLimiter
)
from .data_lifecycle import (
    DataLifecycleManager, get_lifecycle_manager, set_lifecycle_manager,
    start_cleanup_scheduler, stop_cleanup_scheduler
)
from .metrics import (
    get_metrics, get_metrics_content_type, record_price, record_alert,
    record_notification, update_ws_connections, record_ws_message,
    set_system_info, update_db_records, MetricsMiddleware, PROMETHEUS_AVAILABLE
)

logger = logging.getLogger(__name__)

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
            version="0.2.0",
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
    version="0.1.0",
    lifespan=lifespan
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    """首页 - 金价走势图仪表盘"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>金价实时监控系统</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: #f5f5f5;
                color: #333;
                min-height: 100vh;
            }
            .container { max-width: 1200px; margin: 0 auto; padding: 20px; }

            /* 标题区域 */
            .header {
                text-align: center;
                padding: 30px 0;
            }
            .header h1 {
                font-size: 32px;
                color: #1a1a2e;
                margin-bottom: 10px;
            }
            .header .subtitle {
                color: #666;
                font-size: 14px;
            }
            .header .settings-btn {
                position: absolute;
                top: 30px;
                right: 20px;
                background: #fff;
                border: 1px solid #ddd;
                border-radius: 8px;
                padding: 8px 16px;
                cursor: pointer;
                font-size: 14px;
                color: #666;
                transition: all 0.2s;
            }
            .header .settings-btn:hover {
                background: #f5a623;
                color: #fff;
                border-color: #f5a623;
            }
            .header {
                position: relative;
            }

            /* 日期时间显示 */
            .datetime-bar {
                background: #fff;
                border-radius: 8px;
                padding: 15px 30px;
                text-align: center;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .datetime-bar .date { color: #333; font-size: 16px; }
            .datetime-bar .time { color: #f5a623; font-size: 16px; margin-left: 20px; }

            /* 分析切换按钮 */
            .analysis-tabs {
                display: flex;
                justify-content: center;
                gap: 10px;
                margin-bottom: 20px;
            }
            .tab-btn {
                padding: 10px 25px;
                border: none;
                border-radius: 20px;
                cursor: pointer;
                font-size: 14px;
                transition: all 0.3s;
            }
            .tab-btn.active {
                background: #f5a623;
                color: #fff;
            }
            .tab-btn:not(.active) {
                background: #fff;
                color: #666;
            }
            .tab-btn:hover:not(.active) {
                background: #f0f0f0;
            }

            /* AI 分析卡片 */
            .analysis-card {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .analysis-row {
                display: flex;
                margin-bottom: 15px;
            }
            .analysis-label {
                width: 100px;
                color: #333;
                font-weight: 500;
            }
            .analysis-value {
                flex: 1;
                color: #666;
            }
            .analysis-btn {
                display: block;
                width: 150px;
                margin: 20px auto 0;
                padding: 12px 30px;
                background: #f5a623;
                color: #fff;
                border: none;
                border-radius: 25px;
                cursor: pointer;
                font-size: 14px;
                transition: background 0.3s;
            }
            .analysis-btn:hover {
                background: #e09000;
            }

            /* 价格显示区域 */
            .price-section {
                background: #fff;
                border-radius: 8px;
                padding: 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .price-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 20px;
            }
            .price-main {
                display: flex;
                align-items: baseline;
                gap: 15px;
            }
            .price-value {
                font-size: 42px;
                font-weight: bold;
                color: #1a1a2e;
            }
            .price-change {
                font-size: 18px;
                font-weight: 500;
            }
            .price-change.up { color: #00c853; }
            .price-change.down { color: #ff5252; }

            /* 切换按钮组 */
            .switch-group {
                display: flex;
                gap: 5px;
                background: #f5f5f5;
                border-radius: 6px;
                padding: 4px;
            }
            .switch-btn {
                padding: 8px 16px;
                border: none;
                background: transparent;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                color: #666;
                transition: all 0.2s;
            }
            .switch-btn.active {
                background: #fff;
                color: #333;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            .switch-btn:hover:not(.active) {
                color: #333;
            }

            /* 图表控制栏 */
            .chart-controls {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 15px;
                flex-wrap: wrap;
                gap: 15px;
            }

            /* 时间周期选择 */
            .period-group {
                display: flex;
                gap: 5px;
            }
            .period-btn {
                padding: 6px 14px;
                border: 1px solid #ddd;
                background: #fff;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                color: #666;
                transition: all 0.2s;
            }
            .period-btn.active {
                background: #1a1a2e;
                color: #fff;
                border-color: #1a1a2e;
            }
            .period-btn:hover:not(.active) {
                border-color: #999;
            }

            /* 图表容器 */
            #chart {
                width: 100%;
                height: 400px;
            }

            /* 统计信息 */
            .stats-row {
                display: flex;
                justify-content: space-around;
                padding-top: 20px;
                border-top: 1px solid #eee;
                margin-top: 20px;
            }
            .stat-item {
                text-align: center;
            }
            .stat-label {
                font-size: 12px;
                color: #999;
                margin-bottom: 5px;
            }
            .stat-value {
                font-size: 18px;
                font-weight: 500;
                color: #333;
            }
            .stat-value.high { color: #00c853; }
            .stat-value.low { color: #ff5252; }

            /* 历史记录表格 */
            .history-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .history-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            table {
                width: 100%;
                border-collapse: collapse;
            }
            th, td {
                padding: 12px 15px;
                text-align: left;
                border-bottom: 1px solid #eee;
            }
            th {
                color: #999;
                font-weight: 500;
                font-size: 13px;
            }
            td {
                color: #333;
                font-size: 14px;
            }

            /* Markdown 渲染样式 */
            .markdown-content {
                line-height: 1.8;
                color: #333;
            }
            .markdown-content h1, .markdown-content h2, .markdown-content h3, 
            .markdown-content h4, .markdown-content h5, .markdown-content h6 {
                margin: 16px 0 8px 0;
                font-weight: 600;
                color: #1a1a2e;
            }
            .markdown-content h3 { font-size: 16px; }
            .markdown-content h4 { font-size: 14px; }
            .markdown-content p {
                margin: 8px 0;
                line-height: 1.8;
            }
            .markdown-content ul, .markdown-content ol {
                margin: 8px 0 8px 20px;
                padding-left: 10px;
            }
            .markdown-content li {
                margin: 6px 0;
                line-height: 1.6;
            }
            .markdown-content strong {
                font-weight: 600;
                color: #1a1a2e;
            }
            .markdown-content em {
                font-style: italic;
                color: #666;
            }
            .markdown-content code {
                background: #f5f5f5;
                padding: 2px 6px;
                border-radius: 4px;
                font-family: monospace;
                font-size: 13px;
            }
            .markdown-content blockquote {
                border-left: 3px solid #f5a623;
                margin: 12px 0;
                padding: 8px 16px;
                background: #fffaf0;
                color: #666;
            }
            .markdown-content table {
                border-collapse: collapse;
                margin: 12px 0;
                width: 100%;
            }
            .markdown-content th, .markdown-content td {
                border: 1px solid #ddd;
                padding: 8px 12px;
                text-align: left;
            }
            .markdown-content th {
                background: #f5f5f5;
            }
            /* AI分析卡片内的Markdown样式 */
            .analysis-card .markdown-content {
                color: rgba(255,255,255,0.95);
            }
            .analysis-card .markdown-content h3, 
            .analysis-card .markdown-content h4,
            .analysis-card .markdown-content strong {
                color: #fff;
            }
            .analysis-card .markdown-content code {
                background: rgba(255,255,255,0.2);
                color: #fff;
            }
            .analysis-card .markdown-content blockquote {
                border-left-color: rgba(255,255,255,0.5);
                background: rgba(255,255,255,0.1);
                color: rgba(255,255,255,0.9);
            }
            /* 原始响应展示区 */
            .raw-response-section {
                margin-top: 20px;
                border-top: 1px solid #eee;
                padding-top: 15px;
            }
            .raw-response-toggle {
                background: #f5f5f5;
                border: 1px solid #ddd;
                border-radius: 6px;
                padding: 8px 16px;
                cursor: pointer;
                font-size: 13px;
                color: #666;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .raw-response-toggle:hover {
                background: #eee;
            }
            .raw-response-content {
                margin-top: 12px;
                background: #1a1a2e;
                color: #e0e0e0;
                padding: 16px;
                border-radius: 8px;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 13px;
                line-height: 1.6;
                white-space: pre-wrap;
                word-break: break-word;
                max-height: 400px;
                overflow-y: auto;
            }

            /* 加载动画 */
            .loading {
                color: #999;
                font-style: italic;
            }

            /* 银行金价对比 */
            .bank-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .bank-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .section-subtitle {
                font-size: 13px;
                color: #999;
                font-weight: normal;
            }
            .bank-cards {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                gap: 15px;
            }
            .bank-card {
                background: #fff;
                border-radius: 12px;
                padding: 20px;
                color: #333;
                transition: all 0.2s;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
                border: 1px solid #eee;
            }
            .bank-card:hover {
                transform: translateY(-3px);
                box-shadow: 0 6px 20px rgba(0, 0, 0, 0.1);
            }
            .bank-card .bank-name {
                font-size: 15px;
                font-weight: 600;
                margin-bottom: 15px;
                padding-bottom: 10px;
                border-bottom: 2px solid;
            }
            .bank-card .price-row {
                display: flex;
                justify-content: space-between;
                margin-bottom: 8px;
            }
            .bank-card .price-label {
                font-size: 13px;
                color: #888;
            }
            .bank-card .price-value {
                font-size: 18px;
                font-weight: bold;
                color: #333;
            }
            .bank-card .spread {
                font-size: 12px;
                color: #999;
                text-align: right;
                margin-top: 10px;
            }
            .london-gold-info {
                margin-top: 15px;
                padding-top: 15px;
                border-top: 1px solid #eee;
                color: #666;
                font-size: 14px;
            }
            #london-gold-price {
                color: #f5a623;
                font-weight: bold;
            }

            /* 汇率换算工具 */
            .converter-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .converter-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .converter-row {
                display: flex;
                align-items: center;
                gap: 20px;
                flex-wrap: wrap;
            }
            .converter-input-group {
                display: flex;
                gap: 8px;
                flex: 1;
                min-width: 280px;
            }
            .converter-input-group input {
                flex: 1;
                padding: 12px 15px;
                border: 1px solid #ddd;
                border-radius: 6px;
                font-size: 16px;
            }
            .converter-input-group select {
                padding: 12px 10px;
                border: 1px solid #ddd;
                border-radius: 6px;
                background: #fff;
                font-size: 14px;
                cursor: pointer;
            }
            .converter-arrow {
                font-size: 24px;
                color: #999;
            }
            .converter-info {
                margin-top: 15px;
                color: #999;
                font-size: 13px;
            }
            #current-rate {
                color: #f5a623;
                font-weight: bold;
            }

            /* 告警历史 */
            .alerts-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .alerts-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .alert-item {
                display: flex;
                align-items: center;
                padding: 12px 15px;
                border-radius: 8px;
                margin-bottom: 10px;
                background: #f8f9fa;
            }
            .alert-item.threshold_upper { border-left: 4px solid #ff5252; }
            .alert-item.threshold_lower { border-left: 4px solid #ffc107; }
            .alert-item.volatility { border-left: 4px solid #2196f3; }
            .alert-icon {
                font-size: 20px;
                margin-right: 15px;
            }
            .alert-content {
                flex: 1;
            }
            .alert-message {
                font-size: 14px;
                color: #333;
            }
            .alert-time {
                font-size: 12px;
                color: #999;
                margin-top: 4px;
            }
            .alert-price {
                font-size: 16px;
                font-weight: bold;
                color: #333;
            }
            .no-alerts {
                color: #999;
                text-align: center;
                padding: 20px;
            }

            /* 响应式 */
            @media (max-width: 768px) {
                .price-value { font-size: 32px; }
                .chart-controls { flex-direction: column; align-items: stretch; }
                .period-group { justify-content: center; flex-wrap: wrap; }
                #chart { height: 300px; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>黄金价格走势图</h1>
                <p class="subtitle">实时监控金价走势，AI 智能分析市场动态</p>
                <button class="settings-btn" onclick="openSettings()" title="设置">⚙️ 设置</button>
            </div>

            <div class="datetime-bar">
                <span class="date" id="current-date">加载中...</span>
                <span class="time" id="current-time">--:--:--</span>
            </div>

            <!-- 智能分析卡片 -->
            <div class="analysis-card" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 25px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; flex-wrap:wrap; gap:10px;">
                    <h3 style="margin:0; font-size:18px;">📊 AI 智能分析</h3>
                    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                        <select id="analysis-model-select" onchange="onAnalysisModelChange()"
                                style="padding:6px 12px; border-radius:15px; border:none; background:#fff; color:#333; font-size:12px; cursor:pointer; min-width:150px;">
                            <option value="">选择模型...</option>
                        </select>
                        <button onclick="fetchAnalysisModels()" id="fetch-analysis-models-btn"
                                style="padding:6px 12px; background:rgba(255,255,255,0.15); color:#fff; border:none; border-radius:15px; cursor:pointer; font-size:12px;">
                            获取模型
                        </button>
                        <span id="analysis-cache-info" style="font-size:12px; opacity:0.8;"></span>
                        <button onclick="refreshSmartAnalysis()" id="refresh-analysis-btn"
                                style="padding:8px 16px; background:rgba(255,255,255,0.2); color:#fff; border:none; border-radius:20px; cursor:pointer; font-size:13px; transition:all 0.2s;">
                            🔄 刷新分析
                        </button>
                    </div>
                </div>
                
                <div id="ai-status-bar" style="display:flex; align-items:center; gap:8px; padding:8px 12px; border-radius:6px; margin-bottom:15px; font-size:13px; background:rgba(255,255,255,0.15);">
                    <span id="ai-status-icon">●</span>
                    <span id="ai-status-text">检查中...</span>
                </div>
                
                <div id="smart-analysis-content">
                    <div style="text-align:center; padding:30px; opacity:0.8;">
                        <div style="font-size:24px; margin-bottom:10px;">⏳</div>
                        <div>正在获取 AI 分析...</div>
                    </div>
                </div>
                
                <div style="margin-top:15px; text-align:center;">
                    <button onclick="showFullAnalysis()" id="show-full-btn" style="display:none; padding:10px 25px; background:rgba(255,255,255,0.2); color:#fff; border:none; border-radius:20px; cursor:pointer; font-size:14px;">
                        查看完整报告
                    </button>
                </div>
            </div>

            <div class="price-section">
                <div class="price-header">
                    <div class="price-main">
                        <span class="price-value" id="current-price">$0.00</span>
                        <span class="price-change" id="price-change">+0.00%</span>
                    </div>
                    <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                        <div class="switch-group" id="currency-switch">
                            <button class="switch-btn" data-currency="USD">USD</button>
                            <button class="switch-btn active" data-currency="CNY">CNY</button>
                        </div>
                        <div class="switch-group" id="unit-switch">
                            <button class="switch-btn" data-unit="KG">KG</button>
                            <button class="switch-btn active" data-unit="G">G</button>
                            <button class="switch-btn" data-unit="OZ">OZ</button>
                        </div>
                    </div>
                </div>

                <div class="chart-controls">
                    <div></div>
                    <div class="period-group">
                        <button class="period-btn active" data-period="1D">1D</button>
                        <button class="period-btn" data-period="7D">7D</button>
                        <button class="period-btn" data-period="1M">1M</button>
                        <button class="period-btn" data-period="6M">6M</button>
                        <button class="period-btn" data-period="1Y">1Y</button>
                        <button class="period-btn" data-period="5Y">5Y</button>
                    </div>
                </div>

                <div id="chart"></div>

                <div class="stats-row">
                    <div class="stat-item">
                        <div class="stat-label">最高价</div>
                        <div class="stat-value high" id="stat-high">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">最低价</div>
                        <div class="stat-value low" id="stat-low">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">平均价</div>
                        <div class="stat-value" id="stat-avg">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">数据点</div>
                        <div class="stat-value" id="stat-count">0</div>
                    </div>
                </div>
            </div>

            <!-- 银行金价对比 -->
            <div class="bank-section">
                <h2>银行金价对比 <span class="section-subtitle">(Au99.99 人民币/克)</span></h2>
                <div class="bank-cards" id="bank-cards">
                    <div class="loading">加载中...</div>
                </div>
                <div class="london-gold-info">
                    <span>伦敦金基准价: </span>
                    <span id="london-gold-price">¥0.00/克</span>
                </div>
            </div>

            <!-- 汇率换算工具 -->
            <div class="converter-section">
                <h2>金价换算工具</h2>
                <div class="converter-row">
                    <div class="converter-input-group">
                        <input type="number" id="convert-input" value="2050" step="0.01" placeholder="输入金额">
                        <select id="convert-from-unit">
                            <option value="oz">盎司 (oz)</option>
                            <option value="g">克 (g)</option>
                            <option value="kg">公斤 (kg)</option>
                        </select>
                        <select id="convert-from-currency">
                            <option value="USD">美元 (USD)</option>
                            <option value="CNY">人民币 (CNY)</option>
                        </select>
                    </div>
                    <div class="converter-arrow">→</div>
                    <div class="converter-input-group">
                        <input type="text" id="convert-output" readonly placeholder="结果">
                        <select id="convert-to-unit">
                            <option value="oz">盎司 (oz)</option>
                            <option value="g" selected>克 (g)</option>
                            <option value="kg">公斤 (kg)</option>
                        </select>
                        <select id="convert-to-currency">
                            <option value="USD">美元 (USD)</option>
                            <option value="CNY" selected>人民币 (CNY)</option>
                        </select>
                    </div>
                </div>
                <div class="converter-info">
                    当前汇率: 1 USD = <span id="current-rate">7.20</span> CNY
                </div>
            </div>

            <!-- 告警历史 -->
            <div class="alerts-section">
                <h2>最近告警记录</h2>
                <div id="alerts-container">
                    <div class="loading">加载中...</div>
                </div>
            </div>

            <div class="history-section">
                <h2>最近价格记录</h2>
                <table>
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>价格 (USD)</th>
                            <th>来源</th>
                        </tr>
                    </thead>
                    <tbody id="history-table">
                        <tr><td colspan="3" class="loading">加载中...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 详细分析弹窗 -->
        <div id="analysis-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000;">
            <div style="position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; border-radius:12px; padding:30px; max-width:600px; width:90%; max-height:80vh; overflow-y:auto;">
                <h2 style="margin-bottom:20px; color:#333;">AI 分析报告</h2>
                <div id="modal-content"></div>
                <button onclick="closeModal()" style="margin-top:20px; padding:10px 30px; background:#f5a623; color:#fff; border:none; border-radius:20px; cursor:pointer;">关闭</button>
            </div>
        </div>

        <!-- 设置弹窗 - 多平台管理 -->
        <div id="settings-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1001;">
            <div style="position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; border-radius:12px; padding:30px; max-width:700px; width:95%; max-height:85vh; overflow-y:auto;">
                <h2 style="margin-bottom:10px; color:#333; display:flex; align-items:center; gap:10px;">
                    ⚙️ AI 模型服务配置
                </h2>
                <p style="color:#666; font-size:13px; margin-bottom:20px;">
                    管理多个模型服务平台，支持 OpenAI 兼容接口
                </p>
                
                <!-- 平台列表 -->
                <div style="margin-bottom:20px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <label style="font-weight:500; color:#333;">模型服务平台</label>
                        <button type="button" onclick="showAddProviderForm()" 
                                style="padding:6px 12px; background:#4CAF50; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px;">
                            + 添加平台
                        </button>
                    </div>
                    <div id="providers-list" style="border:1px solid #eee; border-radius:8px; overflow:hidden;">
                        <!-- 平台列表由 JS 动态生成 -->
                    </div>
                </div>
                
                <!-- 当前选中平台的配置 -->
                <div id="provider-config-section" style="display:none; background:#f9f9f9; border-radius:8px; padding:20px; margin-bottom:15px;">
                    <h3 style="margin:0 0 15px 0; font-size:16px; color:#333;" id="config-title">配置平台</h3>
                    
                    <div style="margin-bottom:15px;">
                        <label style="display:block; margin-bottom:6px; font-weight:500; color:#555; font-size:13px;">平台名称</label>
                        <input type="text" id="provider-name" placeholder="如: DeepSeek"
                               style="width:100%; padding:10px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box;">
                    </div>
                    
                    <div style="margin-bottom:15px;">
                        <label style="display:block; margin-bottom:6px; font-weight:500; color:#555; font-size:13px;">API 地址</label>
                        <input type="text" id="provider-url" placeholder="如: https://api.deepseek.com"
                               style="width:100%; padding:10px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box;">
                        <p id="provider-url-preview" style="color:#2196f3; font-size:12px; margin-top:3px;"></p>
                    </div>
                    
                    <div style="margin-bottom:15px;">
                        <label style="display:block; margin-bottom:6px; font-weight:500; color:#555; font-size:13px;">API Key</label>
                        <input type="password" id="provider-key" placeholder="输入 API Key（留空保持原有）"
                               style="width:100%; padding:10px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box;">
                    </div>
                    
                    <div style="margin-bottom:15px;">
                        <label style="display:block; margin-bottom:6px; font-weight:500; color:#555; font-size:13px;">选择模型</label>
                        <div style="display:flex; gap:8px;">
                            <select id="provider-model" style="flex:1; padding:10px; border:1px solid #ddd; border-radius:6px; font-size:14px;">
                                <option value="">-- 选择或手动输入 --</option>
                            </select>
                            <button type="button" onclick="fetchProviderModels()" id="fetch-models-btn"
                                    style="padding:10px 14px; background:#2196f3; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px; white-space:nowrap;">
                                获取模型
                            </button>
                        </div>
                        <input type="text" id="provider-model-custom" placeholder="或手动输入: deepseek-chat"
                               style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:13px; box-sizing:border-box; margin-top:8px;">
                    </div>
                    
                    <div style="display:flex; gap:8px; flex-wrap:wrap;">
                        <button type="button" onclick="saveProviderConfig()" 
                                style="flex:1; padding:10px 16px; background:#f5a623; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                            保存配置
                        </button>
                        <button type="button" onclick="testProviderConnection()"
                                style="padding:10px 16px; background:#4CAF50; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                            测试
                        </button>
                        <button type="button" onclick="useThisProvider()"
                                style="padding:10px 16px; background:#9c27b0; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                            使用此配置
                        </button>
                        <button type="button" onclick="deleteCurrentProvider()" id="delete-provider-btn"
                                style="padding:10px 16px; background:#ff5252; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                            删除
                        </button>
                    </div>
                </div>
                
                <div id="settings-status" style="padding:12px; border-radius:8px; margin-bottom:15px; display:none;"></div>
                
                <div style="display:flex; justify-content:flex-end; gap:10px; border-top:1px solid #eee; padding-top:15px;">
                    <button type="button" onclick="resetLLMConfig()" style="padding:10px 20px; background:#ff5252; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                        重置全部
                    </button>
                    <button onclick="closeSettings()" style="padding:10px 20px; background:#666; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:14px;">
                        关闭
                    </button>
                </div>
                
                <button onclick="closeSettings()" style="position:absolute; top:15px; right:15px; background:none; border:none; font-size:24px; cursor:pointer; color:#999;">×</button>
            </div>
        </div>

        <script>
            // 全局状态
            let chart = null;
            let currentCurrency = 'CNY';
            let currentUnit = 'G';
            let currentPeriod = '1D';
            let chartData = { timestamps: [], prices: [] };
            let analysisData = null;

            // 单位换算系数
            const unitConversions = {
                'OZ': 1,
                'G': 31.1035,  // 1 盎司 = 31.1035 克
                'KG': 31103.5  // 1 盎司 = 0.0311035 公斤
            };

            // 汇率（从 API 获取）
            let exchangeRates = {
                'USD': 1,
                'CNY': 7.2
            };

            // 获取实时汇率
            async function fetchExchangeRate() {
                try {
                    const resp = await fetch('/api/exchange-rate');
                    if (resp.ok) {
                        const data = await resp.json();
                        exchangeRates.CNY = data.usd_cny;
                    }
                } catch (e) {
                    console.warn('获取汇率失败，使用默认值');
                }
            }

            // 初始化图表
            function initChart() {
                chart = echarts.init(document.getElementById('chart'));
                window.addEventListener('resize', () => chart.resize());
            }

            // 更新图表
            function updateChart(data) {
                const prices = data.prices.map(p => convertPrice(p));
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const unitLabel = currentUnit === 'OZ' ? '盎司' : (currentUnit === 'G' ? '克' : '公斤');

                const option = {
                    tooltip: {
                        trigger: 'axis',
                        formatter: function(params) {
                            const time = params[0].axisValue;
                            const price = params[0].data;
                            return `${time}<br/>价格: ${currencySymbol}${price.toFixed(2)}/${unitLabel}`;
                        },
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        borderColor: '#eee',
                        borderWidth: 1,
                        textStyle: { color: '#333' }
                    },
                    grid: {
                        left: '3%',
                        right: '4%',
                        bottom: '3%',
                        top: '5%',
                        containLabel: true
                    },
                    xAxis: {
                        type: 'category',
                        data: data.timestamps,
                        boundaryGap: false,
                        axisLine: { lineStyle: { color: '#ddd' } },
                        axisLabel: { color: '#999', fontSize: 11 },
                        axisTick: { show: false }
                    },
                    yAxis: {
                        type: 'value',
                        axisLine: { show: false },
                        axisLabel: {
                            color: '#999',
                            fontSize: 11,
                            formatter: val => currencySymbol + val.toFixed(2)
                        },
                        splitLine: { lineStyle: { color: '#f0f0f0', type: 'dashed' } }
                    },
                    series: [{
                        name: '金价',
                        type: 'line',
                        data: prices,
                        smooth: true,
                        symbol: 'none',
                        lineStyle: {
                            color: '#4ecdc4',
                            width: 2
                        },
                        areaStyle: {
                            color: {
                                type: 'linear',
                                x: 0, y: 0, x2: 0, y2: 1,
                                colorStops: [
                                    { offset: 0, color: 'rgba(78, 205, 196, 0.4)' },
                                    { offset: 1, color: 'rgba(78, 205, 196, 0.05)' }
                                ]
                            }
                        },
                        markLine: {
                            silent: true,
                            symbol: 'none',
                            data: [{
                                yAxis: prices[prices.length - 1],
                                lineStyle: { color: '#4ecdc4', type: 'dashed', width: 1 },
                                label: {
                                    position: 'end',
                                    formatter: currencySymbol + prices[prices.length - 1]?.toFixed(2),
                                    color: '#4ecdc4',
                                    backgroundColor: 'rgba(78, 205, 196, 0.1)',
                                    padding: [4, 8],
                                    borderRadius: 4
                                }
                            }]
                        }
                    }]
                };

                chart.setOption(option);
            }

            // 价格换算
            function convertPrice(priceUSD) {
                const rate = exchangeRates[currentCurrency];
                const unitFactor = unitConversions[currentUnit];
                return (priceUSD * rate) / unitFactor;
            }

            // 获取图表数据
            async function fetchChartData() {
                const periodHours = {
                    '1D': 24,
                    '7D': 168,
                    '1M': 720,
                    '6M': 4320,
                    '1Y': 8760,
                    '5Y': 43800
                };

                const hours = periodHours[currentPeriod] || 24;
                const resp = await fetch(`/api/chart/data?hours=${hours}`);
                const data = await resp.json();
                chartData = data;

                updateChart(data);
                updateStats(data);
                updatePriceDisplay(data);
            }

            // 更新价格显示
            function updatePriceDisplay(data) {
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const price = convertPrice(data.current_price);
                const changePercent = data.price_change_percent;

                document.getElementById('current-price').textContent =
                    currencySymbol + price.toFixed(2);

                const changeEl = document.getElementById('price-change');
                const sign = changePercent >= 0 ? '+' : '';
                changeEl.textContent = sign + changePercent.toFixed(2) + '%';
                changeEl.className = 'price-change ' + (changePercent >= 0 ? 'up' : 'down');
            }

            // 更新统计信息
            function updateStats(data) {
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const high = convertPrice(data.high);
                const low = convertPrice(data.low);
                const prices = data.prices.map(p => convertPrice(p));
                const avg = prices.reduce((a, b) => a + b, 0) / prices.length || 0;

                document.getElementById('stat-high').textContent = currencySymbol + high.toFixed(2);
                document.getElementById('stat-low').textContent = currencySymbol + low.toFixed(2);
                document.getElementById('stat-avg').textContent = currencySymbol + avg.toFixed(2);
                document.getElementById('stat-count').textContent = data.prices.length;
            }

            // 获取历史记录
            async function fetchHistory() {
                const resp = await fetch('/api/price/history?limit=10');
                const data = await resp.json();

                const tbody = document.getElementById('history-table');
                if (data.data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3">暂无数据</td></tr>';
                    return;
                }

                tbody.innerHTML = data.data.map(p => `
                    <tr>
                        <td>${new Date(p.timestamp).toLocaleString('zh-CN')}</td>
                        <td>$${p.price.toFixed(2)}</td>
                        <td>${p.source}</td>
                    </tr>
                `).join('');
            }

            // 智能分析数据
            let smartAnalysisData = null;
            let analysisModels = [];  // 当前平台的模型列表

            // 获取当前平台的模型列表（用于分析）
            async function fetchAnalysisModels() {
                const btn = document.getElementById('fetch-analysis-models-btn');
                btn.textContent = '获取中...';
                btn.disabled = true;
                
                try {
                    // 先获取当前激活的平台
                    const configResp = await fetch('/api/llm/providers');
                    if (!configResp.ok) return;
                    
                    const config = await configResp.json();
                    const activeId = config.active_provider_id;
                    
                    if (!activeId || activeId === 'mock') {
                        alert('当前使用 Mock 模式，请先配置 AI 平台');
                        return;
                    }
                    
                    // 获取模型列表
                    const resp = await fetch(`/api/llm/providers/${activeId}/models`, { method: 'POST' });
                    if (resp.ok) {
                        const data = await resp.json();
                        analysisModels = data.models;
                        
                        // 填充下拉框
                        const select = document.getElementById('analysis-model-select');
                        select.innerHTML = '<option value="">选择模型...</option>';
                        data.models.forEach(m => {
                            const opt = document.createElement('option');
                            opt.value = m.id;
                            opt.textContent = m.id;
                            select.appendChild(opt);
                        });
                        
                        // 如果有活跃模型，选中它
                        if (config.active_model) {
                            select.value = config.active_model;
                        }
                    } else {
                        const error = await resp.json();
                        alert('获取模型失败: ' + (error.detail || '未知错误'));
                    }
                } catch (e) {
                    console.error('获取模型失败', e);
                } finally {
                    btn.textContent = '获取模型';
                    btn.disabled = false;
                }
            }

            // 模型选择变化时
            function onAnalysisModelChange() {
                // 选择模型后可以直接刷新分析
            }

            // 获取 AI 状态
            async function fetchAIStatus() {
                try {
                    const resp = await fetch('/api/llm/status');
                    if (resp.ok) {
                        const status = await resp.json();
                        const statusBar = document.getElementById('ai-status-bar');
                        const icon = document.getElementById('ai-status-icon');
                        const text = document.getElementById('ai-status-text');
                        
                        if (status.enabled) {
                            statusBar.style.background = 'rgba(76, 175, 80, 0.3)';
                            icon.style.color = '#4CAF50';
                            text.innerHTML = `AI 已启用: <strong>${status.provider_name}</strong> - ${status.model}`;
                        } else {
                            statusBar.style.background = 'rgba(255, 152, 0, 0.3)';
                            icon.style.color = '#ff9800';
                            text.innerHTML = `${status.message} <a href="#" onclick="openSettings(); return false;" style="color:#fff; text-decoration:underline;">去配置</a>`;
                        }
                    }
                } catch (e) {
                    console.warn('获取 AI 状态失败', e);
                }
            }

            // 获取智能分析
            async function fetchSmartAnalysis() {
                try {
                    await fetchAIStatus();
                    
                    const resp = await fetch('/api/smart-analysis');
                    if (!resp.ok) {
                        document.getElementById('smart-analysis-content').innerHTML = `
                            <div style="text-align:center; padding:20px; opacity:0.8;">
                                <div style="font-size:24px; margin-bottom:10px;">⚠️</div>
                                <div>获取分析失败，请稍后重试</div>
                            </div>
                        `;
                        return;
                    }
                    
                    smartAnalysisData = await resp.json();
                    renderSmartAnalysis(smartAnalysisData);
                    
                    // 更新缓存信息
                    const cacheInfo = document.getElementById('analysis-cache-info');
                    if (smartAnalysisData.is_cached && smartAnalysisData.cache_age_minutes > 0) {
                        const hours = Math.floor(smartAnalysisData.cache_age_minutes / 60);
                        const mins = smartAnalysisData.cache_age_minutes % 60;
                        cacheInfo.textContent = hours > 0 ? `${hours}小时${mins}分钟前` : `${mins}分钟前`;
                    } else {
                        cacheInfo.textContent = '刚刚更新';
                    }
                    
                    document.getElementById('show-full-btn').style.display = 'inline-block';
                    
                } catch (e) {
                    console.error('获取智能分析失败', e);
                }
            }

            // 渲染 Markdown 内容
            function renderMarkdown(text) {
                if (!text) return '';
                try {
                    return marked.parse(text);
                } catch (e) {
                    console.error('Markdown 解析失败:', e);
                    return text.replace(/\n/g, '<br>');
                }
            }

            // 渲染智能分析
            function renderSmartAnalysis(data) {
                const content = document.getElementById('smart-analysis-content');
                const modelInfo = data.model_used ? `<span style="font-size:11px; opacity:0.6; margin-left:10px;">(${data.model_used})</span>` : '';
                
                content.innerHTML = `
                    <div style="display:grid; gap:15px;">
                        <div>
                            <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">📈 市场概况 ${modelInfo}</div>
                            <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.market_overview)}</div>
                        </div>
                        <div>
                            <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">⏰ 买入时机</div>
                            <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.buy_timing)}</div>
                        </div>
                        <div>
                            <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">💡 操作建议</div>
                            <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.recommendation)}</div>
                        </div>
                    </div>
                `;
            }

            // 刷新智能分析
            async function refreshSmartAnalysis() {
                const btn = document.getElementById('refresh-analysis-btn');
                const originalText = btn.innerHTML;
                btn.innerHTML = '⏳ 分析中...';
                btn.disabled = true;
                
                // 获取选择的模型
                const selectedModel = document.getElementById('analysis-model-select').value;
                
                document.getElementById('smart-analysis-content').innerHTML = `
                    <div style="text-align:center; padding:30px; opacity:0.8;">
                        <div style="font-size:24px; margin-bottom:10px;">🤖</div>
                        <div>AI 正在搜索网络数据并分析，请稍候...</div>
                        <div style="font-size:12px; margin-top:10px; opacity:0.7;">
                            ${selectedModel ? '使用模型: ' + selectedModel : '使用默认模型'} · 这可能需要 10-30 秒
                        </div>
                    </div>
                `;
                
                try {
                    const resp = await fetch('/api/smart-analysis/refresh', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ model: selectedModel || null })
                    });
                    if (resp.ok) {
                        const result = await resp.json();
                        smartAnalysisData = result.data;
                        renderSmartAnalysis(smartAnalysisData);
                        document.getElementById('analysis-cache-info').textContent = '刚刚更新';
                        
                        // 更新模型选择框
                        if (smartAnalysisData.model_used) {
                            const select = document.getElementById('analysis-model-select');
                            // 检查是否存在该选项
                            let found = false;
                            for (let opt of select.options) {
                                if (opt.value === smartAnalysisData.model_used) {
                                    select.value = smartAnalysisData.model_used;
                                    found = true;
                                    break;
                                }
                            }
                        }
                    } else {
                        const error = await resp.json();
                        document.getElementById('smart-analysis-content').innerHTML = `
                            <div style="text-align:center; padding:20px;">
                                <div style="font-size:24px; margin-bottom:10px;">❌</div>
                                <div>分析失败: ${error.detail || '未知错误'}</div>
                            </div>
                        `;
                    }
                } catch (e) {
                    document.getElementById('smart-analysis-content').innerHTML = `
                        <div style="text-align:center; padding:20px;">
                            <div style="font-size:24px; margin-bottom:10px;">❌</div>
                            <div>分析失败: ${e.message}</div>
                        </div>
                    `;
                } finally {
                    btn.innerHTML = originalText;
                    btn.disabled = false;
                }
            }

            // 显示完整分析报告
            function showFullAnalysis() {
                if (!smartAnalysisData) return;
                
                const modal = document.getElementById('analysis-modal');
                const content = document.getElementById('modal-content');
                
                const factors = smartAnalysisData.key_factors.map(f => `<li style="margin-bottom:8px;">${renderMarkdown(f)}</li>`).join('');
                
                // 是否有原始响应
                const hasRawResponse = smartAnalysisData.raw_response && smartAnalysisData.raw_response.length > 0;
                const rawResponseSection = hasRawResponse ? `
                    <div class="raw-response-section">
                        <button class="raw-response-toggle" onclick="toggleRawResponse()">
                            <span id="raw-toggle-icon">▶</span>
                            <span>查看 AI 原始响应</span>
                            <span style="font-size:11px; color:#999;">(${smartAnalysisData.raw_response.length} 字符)</span>
                        </button>
                        <div id="raw-response-content" class="raw-response-content" style="display:none;">${escapeHtml(smartAnalysisData.raw_response)}</div>
                    </div>
                ` : '';
                
                content.innerHTML = `
                    <h3 style="color:#667eea; margin-bottom:15px;">${smartAnalysisData.title}</h3>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">📊 市场概况</h4>
                        <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.market_overview)}</div>
                    </div>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">📈 近期走势</h4>
                        <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.recent_trend)}</div>
                    </div>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">🔍 影响因素</h4>
                        <ul class="markdown-content" style="color:#666; margin-left:20px;">${factors}</ul>
                    </div>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">🔮 价格预测</h4>
                        <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.price_prediction)}</div>
                    </div>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">⏰ 买入时机</h4>
                        <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.buy_timing)}</div>
                    </div>
                    
                    <div style="margin-bottom:20px;">
                        <h4 style="color:#333; margin-bottom:10px;">💡 操作建议</h4>
                        <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.recommendation)}</div>
                    </div>
                    
                    <div style="background:#fff3e0; padding:15px; border-radius:8px; margin-top:20px;">
                        <h4 style="color:#e65100; margin-bottom:10px;">⚠️ 风险提示</h4>
                        <div class="markdown-content" style="color:#e65100; font-size:13px;">${renderMarkdown(smartAnalysisData.risk_warning)}</div>
                    </div>
                    
                    ${rawResponseSection}
                    
                    <p style="color:#999; font-size:12px; margin-top:20px; text-align:right;">
                        生成时间: ${new Date(smartAnalysisData.generated_at).toLocaleString('zh-CN')}
                        ${smartAnalysisData.model_used ? ` · 模型: ${smartAnalysisData.model_used}` : ''}
                    </p>
                `;
                
                modal.style.display = 'block';
            }
            
            // 切换原始响应显示
            function toggleRawResponse() {
                const content = document.getElementById('raw-response-content');
                const icon = document.getElementById('raw-toggle-icon');
                if (content.style.display === 'none') {
                    content.style.display = 'block';
                    icon.textContent = '▼';
                } else {
                    content.style.display = 'none';
                    icon.textContent = '▶';
                }
            }
            
            // HTML 转义（防止 XSS）
            function escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }

            // 兼容旧的 fetchAnalysis（用于其他地方调用）
            async function fetchAnalysis() {
                await fetchSmartAnalysis();
            }

            // 切换分析模式
            function switchAnalysis(mode) {
                document.querySelectorAll('.analysis-tabs .tab-btn').forEach(btn => {
                    btn.classList.remove('active');
                });
                event.target.classList.add('active');
                // 可以根据 mode 加载不同的分析
                fetchAnalysis();
            }

            // 显示详细分析
            function showDetailedAnalysis() {
                const modal = document.getElementById('analysis-modal');
                const content = document.getElementById('modal-content');

                if (analysisData) {
                    content.innerHTML = `
                        <p><strong>市场情绪：</strong> ${analysisData.market_sentiment}</p>
                        <p style="margin-top:15px;"><strong>可能原因：</strong></p>
                        <ul style="margin-left:20px; margin-top:10px;">
                            ${analysisData.possible_reasons.map(r => `<li style="margin-bottom:8px;">${r}</li>`).join('')}
                        </ul>
                        <p style="margin-top:15px;"><strong>操作建议：</strong> ${analysisData.recommendation}</p>
                        <p style="margin-top:15px; color:#999; font-size:12px;">
                            生成时间: ${new Date(analysisData.generated_at).toLocaleString('zh-CN')}
                        </p>
                    `;
                } else {
                    content.innerHTML = '<p>暂无分析数据</p>';
                }

                modal.style.display = 'block';
            }

            // 关闭弹窗
            function closeModal() {
                document.getElementById('analysis-modal').style.display = 'none';
            }

            // 更新日期时间
            function updateDateTime() {
                const now = new Date();
                const year = now.getFullYear();
                const month = String(now.getMonth() + 1).padStart(2, '0');
                const day = String(now.getDate()).padStart(2, '0');
                document.getElementById('current-date').textContent = `${year}年${month}月${day}日`;
                document.getElementById('current-time').textContent =
                    now.toLocaleTimeString('zh-CN', { hour12: false });
            }

            // 绑定事件
            function bindEvents() {
                // 币种切换
                document.querySelectorAll('#currency-switch .switch-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('#currency-switch .switch-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentCurrency = btn.dataset.currency;
                        if (chartData.prices.length > 0) {
                            updateChart(chartData);
                            updateStats(chartData);
                            updatePriceDisplay(chartData);
                        }
                    });
                });

                // 单位切换
                document.querySelectorAll('#unit-switch .switch-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('#unit-switch .switch-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentUnit = btn.dataset.unit;
                        if (chartData.prices.length > 0) {
                            updateChart(chartData);
                            updateStats(chartData);
                            updatePriceDisplay(chartData);
                        }
                    });
                });

                // 时间周期切换
                document.querySelectorAll('.period-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentPeriod = btn.dataset.period;
                        fetchChartData();
                    });
                });

                // 点击弹窗外部关闭
                document.getElementById('analysis-modal').addEventListener('click', (e) => {
                    if (e.target.id === 'analysis-modal') closeModal();
                });

                // 换算工具事件
                const convertInputs = ['convert-input', 'convert-from-unit', 'convert-from-currency', 'convert-to-unit', 'convert-to-currency'];
                convertInputs.forEach(id => {
                    document.getElementById(id).addEventListener('change', doConvert);
                    if (id === 'convert-input') {
                        document.getElementById(id).addEventListener('input', doConvert);
                    }
                });
            }

            // 获取银行金价
            async function fetchBankPrices() {
                try {
                    const resp = await fetch('/api/bank-prices');
                    if (!resp.ok) return;
                    const data = await resp.json();

                    const container = document.getElementById('bank-cards');
                    // 银行品牌色（用于装饰）
                    const bankColors = {
                        '工商银行': '#C80000',
                        '中国银行': '#C2000B',
                        '建设银行': '#0066B3',
                        '农业银行': '#009966',
                        '交通银行': '#003087',
                        '招商银行': '#C41230',
                        '兴业银行': '#0055A5',
                        '民生银行': '#00A0E9',
                        '浦发银行': '#003399',
                        '光大银行': '#7B2D8E',
                        '平安银行': '#FA6400',
                        '中信银行': '#E60012'
                    };
                    const defaultColor = '#666';

                    container.innerHTML = data.data.map((bank, i) => {
                        const color = bankColors[bank.bank_name] || defaultColor;
                        return `
                        <div class="bank-card">
                            <div class="bank-name" style="color: ${color}; border-color: ${color}">${bank.bank_name}</div>
                            <div class="price-row">
                                <span class="price-label">买入价</span>
                                <span class="price-value">¥${bank.buy_price.toFixed(2)}</span>
                            </div>
                            <div class="price-row">
                                <span class="price-label">卖出价</span>
                                <span class="price-value">¥${bank.sell_price.toFixed(2)}</span>
                            </div>
                            <div class="spread">价差: ¥${(bank.sell_price - bank.buy_price).toFixed(2)}</div>
                        </div>`;
                    }).join('');

                    document.getElementById('london-gold-price').textContent = `¥${data.london_gold_cny.toFixed(2)}/克`;
                } catch (e) {
                    console.warn('获取银行金价失败', e);
                }
            }

            // 获取告警历史
            async function fetchAlerts() {
                try {
                    const resp = await fetch('/api/alerts?limit=5');
                    if (!resp.ok) return;
                    const data = await resp.json();

                    const container = document.getElementById('alerts-container');

                    if (data.length === 0) {
                        container.innerHTML = '<div class="no-alerts">暂无告警记录</div>';
                        return;
                    }

                    const icons = {
                        'threshold_upper': '🔴',
                        'threshold_lower': '🟡',
                        'volatility': '⚡'
                    };

                    // 将美元价格转换为人民币（每盎司 -> 每克）
                    const rate = exchangeRates.CNY || 7.2;
                    const ozToGram = 31.1035;

                    container.innerHTML = data.map(alert => {
                        const priceUSD = alert.price;  // USD/oz
                        const priceCNY = (priceUSD * rate) / ozToGram;  // CNY/g
                        return `
                            <div class="alert-item ${alert.alert_type}">
                                <span class="alert-icon">${icons[alert.alert_type] || '⚠️'}</span>
                                <div class="alert-content">
                                    <div class="alert-message">${alert.message}</div>
                                    <div class="alert-time">${new Date(alert.triggered_at).toLocaleString('zh-CN')}</div>
                                </div>
                                <div class="alert-price">¥${priceCNY.toFixed(2)}/克</div>
                            </div>
                        `;
                    }).join('');
                } catch (e) {
                    console.warn('获取告警历史失败', e);
                }
            }

            // 执行换算
            async function doConvert() {
                const price = parseFloat(document.getElementById('convert-input').value) || 0;
                const fromUnit = document.getElementById('convert-from-unit').value;
                const toUnit = document.getElementById('convert-to-unit').value;
                const fromCurrency = document.getElementById('convert-from-currency').value;
                const toCurrency = document.getElementById('convert-to-currency').value;

                try {
                    const resp = await fetch(`/api/convert?price=${price}&from_unit=${fromUnit}&to_unit=${toUnit}&from_currency=${fromCurrency}&to_currency=${toCurrency}`);
                    if (resp.ok) {
                        const data = await resp.json();
                        const symbol = toCurrency === 'USD' ? '$' : '¥';
                        document.getElementById('convert-output').value = `${symbol}${data.converted.price.toFixed(2)}`;
                        document.getElementById('current-rate').textContent = data.exchange_rate.toFixed(4);
                    }
                } catch (e) {
                    console.warn('换算失败', e);
                }
            }

            // ============ WebSocket 实时推送 ============
            let ws = null;
            let wsReconnectTimer = null;
            let wsConnected = false;

            function connectWebSocket() {
                if (ws && ws.readyState === WebSocket.OPEN) return;

                const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${location.host}/ws`;

                console.log('连接 WebSocket:', wsUrl);
                ws = new WebSocket(wsUrl);

                ws.onopen = () => {
                    console.log('WebSocket 已连接');
                    wsConnected = true;
                    updateConnectionStatus(true);
                    // 清除重连定时器
                    if (wsReconnectTimer) {
                        clearTimeout(wsReconnectTimer);
                        wsReconnectTimer = null;
                    }
                    // 启动心跳
                    startHeartbeat();
                };

                ws.onmessage = (event) => {
                    try {
                        const message = JSON.parse(event.data);
                        handleWebSocketMessage(message);
                    } catch (e) {
                        console.warn('WebSocket 消息解析失败:', e);
                    }
                };

                ws.onclose = () => {
                    console.log('WebSocket 已断开');
                    wsConnected = false;
                    updateConnectionStatus(false);
                    // 3秒后重连
                    wsReconnectTimer = setTimeout(connectWebSocket, 3000);
                };

                ws.onerror = (error) => {
                    console.error('WebSocket 错误:', error);
                };
            }

            function handleWebSocketMessage(message) {
                switch (message.type) {
                    case 'price_update':
                        onPriceUpdate(message.data);
                        break;
                    case 'alert':
                        onAlertReceived(message.data);
                        break;
                    case 'pong':
                        // 心跳响应
                        break;
                }
            }

            // 处理实时价格更新
            function onPriceUpdate(data) {
                console.log('收到价格更新:', data.price);

                // 更新当前价格显示
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const price = convertPrice(data.price);
                document.getElementById('current-price').textContent = currencySymbol + price.toFixed(2);

                // 更新图表数据（追加新点）
                if (chartData.timestamps && chartData.prices) {
                    const timestamp = new Date(data.timestamp).toLocaleTimeString('zh-CN');
                    chartData.timestamps.push(timestamp);
                    chartData.prices.push(data.price);
                    chartData.current_price = data.price;

                    // 限制数据点数量
                    if (chartData.timestamps.length > 500) {
                        chartData.timestamps.shift();
                        chartData.prices.shift();
                    }

                    updateChart(chartData);
                }

                // 闪烁效果
                const priceEl = document.getElementById('current-price');
                priceEl.style.transition = 'none';
                priceEl.style.color = '#f5a623';
                setTimeout(() => {
                    priceEl.style.transition = 'color 0.5s';
                    priceEl.style.color = '';
                }, 100);
            }

            // 处理实时告警
            function onAlertReceived(data) {
                console.log('收到告警:', data.message);

                // 显示通知
                showNotification(data.message, data.alert_type);

                // 刷新告警列表
                fetchAlerts();
            }

            // 显示通知
            function showNotification(message, type) {
                // 创建通知元素
                const notification = document.createElement('div');
                notification.className = 'ws-notification ' + (type || 'info');
                notification.innerHTML = `
                    <span class="icon">⚠️</span>
                    <span class="message">${message}</span>
                    <button onclick="this.parentElement.remove()">×</button>
                `;
                notification.style.cssText = `
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    background: linear-gradient(135deg, #ff6b6b, #ee5a5a);
                    color: white;
                    padding: 15px 20px;
                    border-radius: 8px;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.2);
                    z-index: 10000;
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    animation: slideIn 0.3s ease;
                `;

                document.body.appendChild(notification);

                // 5秒后自动消失
                setTimeout(() => notification.remove(), 5000);
            }

            // 更新连接状态
            function updateConnectionStatus(connected) {
                let statusEl = document.getElementById('ws-status');
                if (!statusEl) {
                    statusEl = document.createElement('div');
                    statusEl.id = 'ws-status';
                    statusEl.style.cssText = `
                        position: fixed;
                        bottom: 20px;
                        right: 20px;
                        padding: 8px 15px;
                        border-radius: 20px;
                        font-size: 12px;
                        z-index: 9999;
                        display: flex;
                        align-items: center;
                        gap: 6px;
                    `;
                    document.body.appendChild(statusEl);
                }

                if (connected) {
                    statusEl.style.background = 'linear-gradient(135deg, #4CAF50, #45a049)';
                    statusEl.style.color = 'white';
                    statusEl.innerHTML = '<span style="font-size:8px">●</span> 实时连接';
                } else {
                    statusEl.style.background = 'linear-gradient(135deg, #ff9800, #f57c00)';
                    statusEl.style.color = 'white';
                    statusEl.innerHTML = '<span style="font-size:8px">●</span> 重连中...';
                }
            }

            // 心跳检测
            let heartbeatTimer = null;
            function startHeartbeat() {
                if (heartbeatTimer) clearInterval(heartbeatTimer);
                heartbeatTimer = setInterval(() => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: 'ping' }));
                    }
                }, 30000);
            }

            // ============ 设置相关函数（多平台支持） ============
            
            let llmProviders = [];  // 平台列表
            let activeProviderId = '';  // 当前激活的平台
            let activeModel = '';  // 当前激活的模型
            let editingProviderId = null;  // 正在编辑的平台 ID
            let isAddingNew = false;  // 是否正在添加新平台

            // 打开设置弹窗
            async function openSettings() {
                document.getElementById('settings-modal').style.display = 'block';
                await loadProviders();
            }

            // 关闭设置弹窗
            function closeSettings() {
                document.getElementById('settings-modal').style.display = 'none';
                hideSettingsStatus();
                editingProviderId = null;
                isAddingNew = false;
            }

            // 加载平台列表
            async function loadProviders() {
                try {
                    const resp = await fetch('/api/llm/providers');
                    if (resp.ok) {
                        const data = await resp.json();
                        llmProviders = data.providers;
                        activeProviderId = data.active_provider_id;
                        activeModel = data.active_model;
                        renderProvidersList();
                    }
                } catch (e) {
                    console.warn('加载平台列表失败', e);
                }
            }

            // 渲染平台列表
            function renderProvidersList() {
                const container = document.getElementById('providers-list');
                if (llmProviders.length === 0) {
                    container.innerHTML = '<div style="padding:20px; text-align:center; color:#999;">暂无平台配置</div>';
                    return;
                }
                
                container.innerHTML = llmProviders.map(p => {
                    const isActive = p.id === activeProviderId;
                    const hasKey = p.has_api_key;
                    return `
                        <div class="provider-item" style="display:flex; align-items:center; padding:12px 15px; border-bottom:1px solid #eee; cursor:pointer; ${isActive ? 'background:#f0f7ff;' : ''}"
                             onclick="selectProvider('${p.id}')">
                            <div style="flex:1;">
                                <div style="font-weight:500; color:#333; display:flex; align-items:center; gap:8px;">
                                    ${p.name}
                                    ${isActive ? '<span style="background:#4CAF50; color:#fff; font-size:10px; padding:2px 6px; border-radius:10px;">当前使用</span>' : ''}
                                </div>
                                <div style="font-size:12px; color:#999; margin-top:3px;">
                                    ${p.base_url || '(无 URL)'}
                                    ${hasKey ? ' • <span style="color:#4CAF50;">已配置 Key</span>' : ' • <span style="color:#ff9800;">未配置 Key</span>'}
                                </div>
                            </div>
                            <div style="color:#ccc; font-size:20px;">›</div>
                        </div>
                    `;
                }).join('');
            }

            // 选择平台进行编辑
            function selectProvider(providerId) {
                isAddingNew = false;
                editingProviderId = providerId;
                const provider = llmProviders.find(p => p.id === providerId);
                if (!provider) return;
                
                document.getElementById('provider-config-section').style.display = 'block';
                document.getElementById('config-title').textContent = `配置: ${provider.name}`;
                document.getElementById('provider-name').value = provider.name;
                document.getElementById('provider-url').value = provider.base_url || '';
                document.getElementById('provider-key').value = '';
                document.getElementById('provider-key').placeholder = provider.has_api_key ? `已配置 (${provider.api_key})` : '输入 API Key';
                
                // 填充模型下拉框
                const modelSelect = document.getElementById('provider-model');
                modelSelect.innerHTML = '<option value="">-- 选择或手动输入 --</option>';
                (provider.models || []).forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m;
                    opt.textContent = m;
                    modelSelect.appendChild(opt);
                });
                
                // 如果是当前激活的平台，显示已选模型
                if (providerId === activeProviderId && activeModel) {
                    let found = false;
                    for (let opt of modelSelect.options) {
                        if (opt.value === activeModel) {
                            modelSelect.value = activeModel;
                            found = true;
                            break;
                        }
                    }
                    if (!found) {
                        document.getElementById('provider-model-custom').value = activeModel;
                    }
                } else {
                    document.getElementById('provider-model-custom').value = '';
                }
                
                // Mock 不可删除
                document.getElementById('delete-provider-btn').style.display = providerId === 'mock' ? 'none' : 'inline-block';
                
                updateProviderUrlPreview();
            }

            // 显示添加平台表单
            function showAddProviderForm() {
                isAddingNew = true;
                editingProviderId = null;
                
                document.getElementById('provider-config-section').style.display = 'block';
                document.getElementById('config-title').textContent = '添加新平台';
                document.getElementById('provider-name').value = '';
                document.getElementById('provider-url').value = '';
                document.getElementById('provider-key').value = '';
                document.getElementById('provider-key').placeholder = '输入 API Key';
                document.getElementById('provider-model').innerHTML = '<option value="">-- 选择或手动输入 --</option>';
                document.getElementById('provider-model-custom').value = '';
                document.getElementById('delete-provider-btn').style.display = 'none';
                document.getElementById('provider-url-preview').textContent = '';
            }

            // 更新 URL 预览
            function updateProviderUrlPreview() {
                const baseUrl = document.getElementById('provider-url').value.trim();
                const preview = document.getElementById('provider-url-preview');
                
                if (!baseUrl) {
                    preview.textContent = '';
                    return;
                }
                
                let url = baseUrl.replace(/[/]+$/, '');
                if (!url.endsWith('/v1') && !url.includes('/v1/')) {
                    url = url + '/v1';
                }
                preview.textContent = `预览: ${url}/chat/completions`;
            }

            // 显示设置状态
            function showSettingsStatus(message, isSuccess) {
                const status = document.getElementById('settings-status');
                status.style.display = 'block';
                status.style.background = isSuccess ? '#e8f5e9' : '#ffebee';
                status.style.color = isSuccess ? '#2e7d32' : '#c62828';
                status.innerHTML = message;
            }

            // 隐藏设置状态
            function hideSettingsStatus() {
                document.getElementById('settings-status').style.display = 'none';
            }

            // 保存平台配置
            async function saveProviderConfig() {
                const name = document.getElementById('provider-name').value.trim();
                const url = document.getElementById('provider-url').value.trim();
                const key = document.getElementById('provider-key').value;
                
                if (!name) {
                    showSettingsStatus('❌ 请输入平台名称', false);
                    return;
                }
                
                try {
                    let resp;
                    if (isAddingNew) {
                        // 添加新平台
                        resp = await fetch('/api/llm/providers', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ name, base_url: url, api_key: key || null })
                        });
                        
                        if (resp.ok) {
                            const data = await resp.json();
                            showSettingsStatus('✅ 保存成功！', true);
                            // 重要：保存成功后切换为编辑模式
                            isAddingNew = false;
                            editingProviderId = data.provider.id;
                            await loadProviders();
                            selectProvider(data.provider.id);
                        } else {
                            const error = await resp.json();
                            showSettingsStatus(`❌ 保存失败: ${error.detail || '未知错误'}`, false);
                        }
                    } else {
                        // 更新现有平台
                        resp = await fetch(`/api/llm/providers/${editingProviderId}`, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ name, base_url: url, api_key: key || null })
                        });
                        
                        if (resp.ok) {
                            showSettingsStatus('✅ 保存成功！', true);
                            await loadProviders();
                            selectProvider(editingProviderId);
                        } else {
                            const error = await resp.json();
                            showSettingsStatus(`❌ 保存失败: ${error.detail || '未知错误'}`, false);
                        }
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 保存失败: ${e.message}`, false);
                }
            }

            // 获取平台模型列表
            async function fetchProviderModels() {
                if (!editingProviderId && !isAddingNew) return;
                
                const btn = document.getElementById('fetch-models-btn');
                const originalText = btn.textContent;
                btn.textContent = '获取中...';
                btn.disabled = true;
                
                // 如果是新平台，先保存
                if (isAddingNew) {
                    showSettingsStatus('❌ 请先保存平台配置', false);
                    btn.textContent = originalText;
                    btn.disabled = false;
                    return;
                }
                
                try {
                    const resp = await fetch(`/api/llm/providers/${editingProviderId}/models`, {
                        method: 'POST'
                    });
                    
                    if (resp.ok) {
                        const data = await resp.json();
                        const select = document.getElementById('provider-model');
                        const currentValue = select.value || document.getElementById('provider-model-custom').value;
                        
                        select.innerHTML = '<option value="">-- 选择模型 --</option>';
                        data.models.forEach(m => {
                            const opt = document.createElement('option');
                            opt.value = m.id;
                            opt.textContent = m.id + (m.owned_by ? ` (${m.owned_by})` : '');
                            select.appendChild(opt);
                        });
                        
                        // 恢复之前选中的值
                        if (currentValue) {
                            for (let opt of select.options) {
                                if (opt.value === currentValue) {
                                    select.value = currentValue;
                                    break;
                                }
                            }
                        }
                        
                        showSettingsStatus(`✅ 获取到 ${data.count} 个模型`, true);
                        await loadProviders();  // 刷新缓存
                    } else {
                        const error = await resp.json();
                        showSettingsStatus(`❌ 获取失败: ${error.detail || '未知错误'}`, false);
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 获取失败: ${e.message}`, false);
                } finally {
                    btn.textContent = originalText;
                    btn.disabled = false;
                }
            }

            // 测试平台连接
            async function testProviderConnection() {
                if (!editingProviderId) {
                    showSettingsStatus('❌ 请先保存平台配置', false);
                    return;
                }
                
                // 获取用户选择的模型
                const modelSelect = document.getElementById('provider-model').value;
                const modelCustom = document.getElementById('provider-model-custom').value;
                const model = modelSelect || modelCustom || null;
                
                showSettingsStatus('⏳ 正在测试连接...', true);
                
                try {
                    const resp = await fetch(`/api/llm/providers/${editingProviderId}/test`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ model })
                    });
                    const result = await resp.json();
                    
                    if (result.success) {
                        showSettingsStatus(`✅ ${result.message}${result.response ? '<br><small>' + result.response + '</small>' : ''}`, true);
                    } else {
                        showSettingsStatus(`❌ ${result.message}`, false);
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 测试失败: ${e.message}`, false);
                }
            }

            // 使用此平台
            async function useThisProvider() {
                if (!editingProviderId) return;
                
                const modelSelect = document.getElementById('provider-model').value;
                const modelCustom = document.getElementById('provider-model-custom').value;
                const model = modelSelect || modelCustom || '';
                
                try {
                    const resp = await fetch('/api/llm/active', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ provider_id: editingProviderId, model })
                    });
                    
                    if (resp.ok) {
                        showSettingsStatus('✅ 已切换到此平台，AI 分析将使用新配置', true);
                        await loadProviders();
                        // 刷新主页面的 AI 状态
                        await fetchAIStatus();
                    } else {
                        const error = await resp.json();
                        showSettingsStatus(`❌ 切换失败: ${error.detail || '未知错误'}`, false);
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 切换失败: ${e.message}`, false);
                }
            }

            // 删除当前平台
            async function deleteCurrentProvider() {
                if (!editingProviderId || editingProviderId === 'mock') return;
                
                const provider = llmProviders.find(p => p.id === editingProviderId);
                if (!confirm(`确定要删除平台 "${provider?.name}" 吗？`)) return;
                
                try {
                    const resp = await fetch(`/api/llm/providers/${editingProviderId}`, {
                        method: 'DELETE'
                    });
                    
                    if (resp.ok) {
                        showSettingsStatus('✅ 平台已删除', true);
                        document.getElementById('provider-config-section').style.display = 'none';
                        editingProviderId = null;
                        await loadProviders();
                    } else {
                        const error = await resp.json();
                        showSettingsStatus(`❌ 删除失败: ${error.detail || '未知错误'}`, false);
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 删除失败: ${e.message}`, false);
                }
            }

            // 重置 LLM 配置
            async function resetLLMConfig() {
                if (!confirm('确定要重置所有配置吗？将恢复默认设置。')) return;
                
                try {
                    const resp = await fetch('/api/llm/config', { method: 'DELETE' });
                    if (resp.ok) {
                        showSettingsStatus('✅ 配置已重置', true);
                        document.getElementById('provider-config-section').style.display = 'none';
                        editingProviderId = null;
                        await loadProviders();
                    } else {
                        showSettingsStatus('❌ 重置失败', false);
                    }
                } catch (e) {
                    showSettingsStatus(`❌ 重置失败: ${e.message}`, false);
                }
            }

            // 初始化
            document.addEventListener('DOMContentLoaded', async () => {
                initChart();
                bindEvents();
                updateDateTime();
                setInterval(updateDateTime, 1000);

                // 初始化 WebSocket 连接
                connectWebSocket();

                // 绑定设置相关事件
                document.getElementById('provider-url').addEventListener('input', updateProviderUrlPreview);
                document.getElementById('settings-modal').addEventListener('click', (e) => {
                    if (e.target.id === 'settings-modal') closeSettings();
                });

                await Promise.all([
                    fetchExchangeRate(),
                    fetchChartData(),
                    fetchHistory(),
                    fetchAnalysis(),
                    fetchBankPrices(),
                    fetchAlerts()
                ]);

                // 初始换算
                doConvert();
                
                // 加载 AI 状态和模型列表
                fetchAIStatus();
                fetchSmartAnalysis();
                fetchAnalysisModels();

                // 备用轮询（WebSocket 断开时使用，降低频率）
                setInterval(() => {
                    if (!wsConnected) fetchChartData();
                }, 30000);
                setInterval(fetchHistory, 60000);           // 1分钟更新历史
                setInterval(fetchExchangeRate, 1800000);    // 30分钟更新汇率
                setInterval(fetchBankPrices, 60000);        // 1分钟更新银行金价
                setInterval(fetchAlerts, 60000);            // 1分钟更新告警（WebSocket 补充）
            });
        </script>
    </body>
    </html>
    """


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
    """手动触发数据间隙补全"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")
    
    filled = await collector.fill_gaps(hours)
    
    return {
        "message": f"已补全 {filled} 条数据",
        "filled_count": filled,
        "gaps_detected": collector.stats.gaps_detected,
        "gaps_filled": collector.stats.gaps_filled
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
                "model_used": model_used
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
        "message": f"已启用 AI 分析",
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
async def reset_llm_config():
    """重置 LLM 配置"""
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
async def cleanup_data():
    """执行数据清理（需要管理员权限）"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")
    
    result = await manager.cleanup()
    return result


@app.post("/api/data/backup")
async def backup_data(backup_name: Optional[str] = None):
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
