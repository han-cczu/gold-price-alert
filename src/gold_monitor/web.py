"""FastAPI Web 服务 - 纯 Web 模式，自动数据采集 + WebSocket 实时推送

本模块是应用装配层（application shell）：
- 创建 FastAPI `app`、配置 CORS / 中间件 / lifespan；
- 提供首页 `/`（HTML 见 static/index.html）；
- include 各领域 router（见 `routers/` 包）。

具体端点逻辑已按领域拆分到 `routers/`，跨 router 共享的全局状态、
WebSocket 管理器、鉴权依赖、缓存与智能分析逻辑见 `state.py`。

为保持向后兼容，`app` 与 `db` 仍可从本模块导入
（`from gold_monitor.web import app, db`）。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import __version__
from .config import settings
from .collector import AdvancedCollector, FetchStrategy, get_collector, set_collector
from .alert import AlertMonitor
from .security import is_admin_path, is_rate_limited_path
from .data_lifecycle import (
    DataLifecycleManager, set_lifecycle_manager,
    start_cleanup_scheduler, stop_cleanup_scheduler
)
from .metrics import (
    set_system_info, MetricsMiddleware, PROMETHEUS_AVAILABLE
)
from . import routers
from .state import (
    db,
    get_auth,
    get_limiter,
    on_price_update,
    set_alert_monitor,
    get_alert_monitor,
    set_daily_analysis_task,
    get_daily_analysis_task,
    get_smart_analysis_cache,
    _run_smart_analysis,
    _daily_analysis_scheduler,
)

logger = logging.getLogger(__name__)

# 首页 HTML（已抽离到 static/index.html，模块加载时读入一次）
_INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

# 重新导出 db，保持 `from gold_monitor.web import app, db` 向后兼容
__all__ = ["app", "db", "run_server"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("金价监控系统启动")

    # 初始化告警监控器（带状态持久化）
    alert_monitor = AlertMonitor(
        database=db,
        channels=[],
        threshold_upper=settings.alert_price_upper,
        threshold_lower=settings.alert_price_lower,
        volatility_percent=settings.alert_threshold_percent,
        volatility_window_minutes=settings.alert_volatility_window,
        use_smart_volatility=True,  # 使用抗噪波动检测
        persist_interval=60  # 每60秒持久化状态
    )
    set_alert_monitor(alert_monitor)

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
    set_daily_analysis_task(asyncio.create_task(_daily_analysis_scheduler()))
    logger.info("每日智能分析定时任务已启动（每天0点执行）")

    # 启动时执行一次智能分析（如果没有缓存）
    if get_smart_analysis_cache() is None:
        asyncio.create_task(_run_smart_analysis())

    yield

    # 关闭时
    logger.info("金价监控系统关闭")

    # 强制持久化告警状态
    active_alert_monitor = get_alert_monitor()
    if active_alert_monitor:
        active_alert_monitor.force_persist()
        # 等待后台通知任务完成，避免关停时丢失正在发送的通知
        await active_alert_monitor.wait_pending_notifications()

    # 停止定时清理任务
    stop_cleanup_scheduler()

    # 取消定时任务
    daily_analysis_task = get_daily_analysis_task()
    if daily_analysis_task:
        daily_analysis_task.cancel()
        try:
            await daily_analysis_task
        except asyncio.CancelledError:
            pass

    active_collector = get_collector()
    if active_collector:
        await active_collector.stop()


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


# ============ 首页 ============

@app.get("/", response_class=HTMLResponse)
async def root():
    """首页 - 金价走势图仪表盘（HTML 见 static/index.html）"""
    return _INDEX_HTML


# ============ 注册各领域 router ============

app.include_router(routers.system.router)
app.include_router(routers.price.router)
app.include_router(routers.alerts.router)
app.include_router(routers.collector.router)
app.include_router(routers.analysis.router)
app.include_router(routers.llm.router)
app.include_router(routers.market.router)
app.include_router(routers.notifications.router)
app.include_router(routers.data.router)
app.include_router(routers.source_quality.router)


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """启动 Web 服务"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
