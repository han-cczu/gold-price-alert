"""日志配置模块 - 支持脱敏处理"""

import logging
import re
import sys
from pathlib import Path
from typing import Optional


class SensitiveDataFilter(logging.Filter):
    """敏感数据脱敏过滤器

    自动脱敏以下内容：
    - API Key
    - 邮箱地址
    - 手机号码
    - 密码字段
    - Token
    """

    # 脱敏规则
    PATTERNS = [
        # API Key (常见格式)
        (
            r'(api[_-]?key|apikey|access[_-]?token|bearer)\s*[=:]\s*["\']?([a-zA-Z0-9_-]{20,})["\']?',
            r"\1=***REDACTED***",
        ),
        # Anthropic API Key
        (r"(sk-ant-[a-zA-Z0-9_-]{20,})", r"sk-ant-***REDACTED***"),
        # OpenAI API Key
        (r"(sk-[a-zA-Z0-9]{20,})", r"sk-***REDACTED***"),
        # 邮箱地址
        (r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", r"***@\2"),
        # 手机号码 (中国)
        (r"(1[3-9]\d)\d{4}(\d{4})", r"\1****\2"),
        # 密码字段
        (
            r'(password|passwd|pwd|secret)\s*[=:]\s*["\']?([^"\'\s,}]+)["\']?',
            r"\1=***REDACTED***",
        ),
        # Bot Token
        (r"(\d{9,}:[a-zA-Z0-9_-]{30,})", r"***BOT_TOKEN***"),
        # 通用 Token
        (
            r'(token|auth)\s*[=:]\s*["\']?([a-zA-Z0-9_.-]{20,})["\']?',
            r"\1=***REDACTED***",
        ),
    ]

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._compiled_patterns = [
            (re.compile(pattern, re.IGNORECASE), replacement)
            for pattern, replacement in self.PATTERNS
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        """过滤并脱敏日志记录"""
        if record.msg:
            record.msg = self._sanitize(str(record.msg))
        if record.args:
            record.args = tuple(
                self._sanitize(str(arg)) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True

    def _sanitize(self, text: str) -> str:
        """脱敏文本"""
        for pattern, replacement in self._compiled_patterns:
            text = pattern.sub(replacement, text)
        return text


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器（控制台用）"""

    COLORS = {
        "DEBUG": "\033[36m",  # 青色
        "INFO": "\033[32m",  # 绿色
        "WARNING": "\033[33m",  # 黄色
        "ERROR": "\033[31m",  # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    enable_color: bool = True,
    enable_sanitize: bool = True,
) -> logging.Logger:
    """配置日志系统

    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件路径（可选）
        enable_color: 是否启用彩色输出
        enable_sanitize: 是否启用敏感数据脱敏

    Returns:
        配置好的 Logger 实例
    """
    # 获取根日志器
    logger = logging.getLogger("gold_monitor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有处理器
    logger.handlers.clear()

    # 日志格式
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s"
    )
    date_format = "%Y-%m-%d %H:%M:%S"

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)

    if enable_color and sys.stdout.isatty():
        console_handler.setFormatter(ColoredFormatter(log_format, date_format))
    else:
        console_handler.setFormatter(logging.Formatter(log_format, date_format))

    if enable_sanitize:
        console_handler.addFilter(SensitiveDataFilter())

    logger.addHandler(console_handler)

    # 文件处理器（可选）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format, date_format))

        if enable_sanitize:
            file_handler.addFilter(SensitiveDataFilter())

        logger.addHandler(file_handler)

    return logger


# 默认日志器
logger = setup_logging()


def get_logger(name: str = "gold_monitor") -> logging.Logger:
    """获取子日志器"""
    return logging.getLogger(name)


# 便捷函数
def debug(msg: str, *args, **kwargs):
    logger.debug(msg, *args, **kwargs)


def info(msg: str, *args, **kwargs):
    logger.info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs):
    logger.warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs):
    logger.error(msg, *args, **kwargs)


def critical(msg: str, *args, **kwargs):
    logger.critical(msg, *args, **kwargs)
