"""FastAPI application factory and backwards-compatible ASGI entry point."""

from contextlib import asynccontextmanager
from pathlib import Path
import warnings

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, routers
from .config import Settings
from .llm_config import LLMConfigPersistenceError, LLMConfigLoadError
from .metrics import MetricsMiddleware, set_system_info
from .runtime import ApplicationRuntime
from .security import is_admin_path, is_rate_limited_path
from .services.prices import PriceUnavailableError

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: Settings | None = None,
    *,
    runtime_factory=ApplicationRuntime,
    **runtime_options,
) -> FastAPI:
    config = (config if config is not None else Settings()).model_copy(deep=True)
    runtime = runtime_factory(config, **runtime_options)

    @asynccontextmanager
    async def lifespan(application):
        await runtime.start()
        try:
            set_system_info(
                version=__version__,
                data_source=config.data_source,
                fetch_interval=config.fetch_interval,
            )
            yield
        finally:
            await runtime.close()

    application = FastAPI(
        title="金价实时监控系统",
        description="实时获取金价、告警通知、AI 分析",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.runtime = runtime
    application.state.settings = config
    origins = [
        origin.strip()
        for origin in config.cors_allow_origins.split(",")
        if origin.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=bool(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(MetricsMiddleware)

    @application.middleware("http")
    async def security_middleware(request: Request, call_next):
        if is_rate_limited_path(request.url.path):
            allowed, _ = runtime.limiter.is_allowed(request)
            if not allowed:
                return JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试"},
                    status_code=429,
                    headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
                )
        if is_admin_path(request.url.path) and config.enable_auth:
            if not runtime.auth.verify_admin_key(request):
                return JSONResponse(
                    {"detail": "需要管理员权限，请提供有效的 X-Admin-Key 头"},
                    status_code=401,
                    headers={"WWW-Authenticate": "API-Key"},
                )
        return await call_next(request)

    @application.exception_handler(LLMConfigPersistenceError)
    async def config_failure(request, exc):
        return JSONResponse(
            {"detail": "模型配置保存失败，原有配置未改变"}, status_code=503
        )

    @application.exception_handler(LLMConfigLoadError)
    async def config_load_failure(request, exc):
        return JSONResponse(
            {"detail": "模型配置无法读取，请检查配置文件及主密钥"}, status_code=503
        )

    @application.exception_handler(PriceUnavailableError)
    async def source_failure(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @application.exception_handler(ValueError)
    async def invalid_configuration(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @application.get("/", response_class=HTMLResponse)
    async def root():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for module in (
        routers.system,
        routers.price,
        routers.alerts,
        routers.collector,
        routers.analysis,
        routers.llm,
        routers.market,
        routers.notifications,
        routers.data,
        routers.source_quality,
    ):
        application.include_router(module.router)
    return application


app = create_app()


def __getattr__(name):
    if name == "db":
        warnings.warn(
            "web.db is deprecated; use app.state.runtime.db inside lifespan",
            DeprecationWarning,
            stacklevel=2,
        )
        return app.state.runtime.db
    raise AttributeError(name)


def run_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn

    uvicorn.run("gold_monitor.web:app", host=host, port=port)


__all__ = ["app", "create_app", "run_server"]
