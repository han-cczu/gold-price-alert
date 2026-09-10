"""配置管理模块"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """应用配置"""

    # 数据库配置
    database_url: str = Field(
        default="sqlite:///gold_prices.db", description="数据库连接URL"
    )

    # 数据采集配置
    fetch_interval: int = Field(default=30, description="数据采集间隔（秒）")
    price_heartbeat_seconds: int = Field(
        default=300, ge=1, le=3600, description="同价报价最长保存间隔（秒）"
    )

    # 数据源配置
    data_source: str = Field(
        default="mock", description="数据源类型: mock, sina, goldapi"
    )

    # GoldAPI配置（可选）
    goldapi_key: str = Field(default="", description="GoldAPI.io API Key")

    # 告警配置
    alert_threshold_percent: float = Field(
        default=1.0, description="价格波动告警阈值（百分比）"
    )
    alert_price_upper: float = Field(default=2500.0, description="价格上限告警")
    alert_price_lower: float = Field(default=1800.0, description="价格下限告警")
    alert_volatility_window: int = Field(
        default=5, description="波动检测时间窗口（分钟）"
    )

    # 邮件通知配置
    smtp_host: str = Field(default="", description="SMTP服务器地址")
    smtp_port: int = Field(default=587, description="SMTP端口")
    smtp_username: str = Field(default="", description="SMTP用户名")
    smtp_password: str = Field(default="", description="SMTP密码")
    alert_email_to: str = Field(default="", description="告警接收邮箱，多个用逗号分隔")

    # Webhook通知配置
    webhook_url: str = Field(default="", description="Webhook URL")
    webhook_type: str = Field(
        default="generic", description="Webhook类型: generic, dingtalk, wechat"
    )

    # Telegram 通知配置
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token")
    telegram_chat_id: str = Field(default="", description="Telegram Chat ID")

    # 大模型配置
    llm_provider: str = Field(
        default="anthropic", description="大模型提供商: anthropic, openai"
    )
    anthropic_api_key: str = Field(default="", description="Anthropic API Key")
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    openai_base_url: str = Field(
        default="", description="OpenAI API Base URL（用于兼容接口）"
    )
    tavily_api_key: str = Field(
        default="",
        description="Tavily 搜索 API Key（启用 DeepSeek/兼容接口的真实联网搜索）",
    )
    llm_config_path: str = Field(default="llm_config.json", description="模型配置文件")
    encrypt_api_keys: bool = Field(default=False, description="加密保存模型密钥")

    # 安全配置
    secret_key: str = Field(default="", description="主密钥（用于加密存储）")
    admin_api_key: str = Field(default="", description="管理接口 API Key")
    rate_limit_per_minute: int = Field(default=60, description="每分钟请求限制")
    trusted_proxy_ips: str = Field(
        default="127.0.0.1", description="可信反向代理 IP/CIDR，供 Uvicorn 解析转发地址"
    )
    enable_auth: bool = Field(default=False, description="是否启用接口鉴权")
    cors_allow_origins: str = Field(
        default="",
        description="CORS 允许的来源，逗号分隔；为空表示仅同源（不允许跨域携带凭证）",
    )

    # 数据生命周期配置
    data_retention_days: int = Field(default=30, description="分钟级数据保留天数")
    hourly_aggregation_days: int = Field(
        default=90, description="兼容保留项：小时级聚合尚未实现"
    )
    daily_aggregation_days: int = Field(
        default=365, description="兼容保留项：日级聚合尚未实现"
    )
    backup_enabled: bool = Field(default=False, description="是否启用自动备份")
    backup_interval_hours: int = Field(
        default=24, ge=1, le=168, description="自动备份间隔（小时）"
    )
    backup_keep_count: int = Field(
        default=7, ge=1, le=365, description="自动备份保留数量，不影响手动备份"
    )
    backup_path: str = Field(default="./backups", description="备份文件路径")

    model_config = SettingsConfigDict(env_prefix="GOLD_", env_file=".env")


settings = Settings()
