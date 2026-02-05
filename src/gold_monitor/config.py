"""配置管理模块"""

from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


class Settings(BaseSettings):
    """应用配置"""

    # 数据库配置
    database_url: str = Field(
        default="sqlite:///gold_prices.db",
        description="数据库连接URL"
    )

    # 数据采集配置
    fetch_interval: int = Field(
        default=30,
        description="数据采集间隔（秒）"
    )

    # 数据源配置
    data_source: str = Field(
        default="mock",
        description="数据源类型: mock, sina, goldapi"
    )

    # GoldAPI配置（可选）
    goldapi_key: str = Field(
        default="",
        description="GoldAPI.io API Key"
    )

    # 告警配置
    alert_threshold_percent: float = Field(
        default=1.0,
        description="价格波动告警阈值（百分比）"
    )
    alert_price_upper: float = Field(
        default=2500.0,
        description="价格上限告警"
    )
    alert_price_lower: float = Field(
        default=1800.0,
        description="价格下限告警"
    )
    alert_volatility_window: int = Field(
        default=5,
        description="波动检测时间窗口（分钟）"
    )

    # 邮件通知配置
    smtp_host: str = Field(default="", description="SMTP服务器地址")
    smtp_port: int = Field(default=587, description="SMTP端口")
    smtp_username: str = Field(default="", description="SMTP用户名")
    smtp_password: str = Field(default="", description="SMTP密码")
    alert_email_to: str = Field(default="", description="告警接收邮箱，多个用逗号分隔")

    # Webhook通知配置
    webhook_url: str = Field(default="", description="Webhook URL")
    webhook_type: str = Field(default="generic", description="Webhook类型: generic, dingtalk, wechat")

    # Telegram 通知配置
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token")
    telegram_chat_id: str = Field(default="", description="Telegram Chat ID")

    # 大模型配置
    llm_provider: str = Field(default="anthropic", description="大模型提供商: anthropic, openai")
    anthropic_api_key: str = Field(default="", description="Anthropic API Key")
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    openai_base_url: str = Field(default="", description="OpenAI API Base URL（用于兼容接口）")

    class Config:
        env_prefix = "GOLD_"
        env_file = ".env"


settings = Settings()
