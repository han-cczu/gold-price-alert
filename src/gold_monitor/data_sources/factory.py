"""Construct sources from the configuration of one application instance."""

from ..config import Settings, settings
from .base import BaseDataSource
from .fallback import FallbackDataSource
from .goldapi import GoldAPIDataSource
from .mock import MockDataSource
from .sina import SinaDataSource


def create_data_source(
    source_type: str | None = None, config: Settings | None = None
) -> BaseDataSource:
    """创建数据源实例"""
    config = config if config is not None else settings
    source_type = source_type or config.data_source

    if source_type == "mock":
        return MockDataSource()
    elif source_type == "sina":
        return SinaDataSource()
    elif source_type == "goldapi":
        if not config.goldapi_key:
            raise ValueError("GoldAPI 需要配置 API Key")
        return GoldAPIDataSource(config.goldapi_key)
    elif source_type == "fallback":
        return create_fallback_source(config)
    else:
        raise ValueError(f"不支持的数据源类型: {source_type}")


def create_fallback_source(config: Settings | None = None) -> FallbackDataSource:
    """创建带故障自动切换的数据源"""
    config = config if config is not None else settings
    sources: list[BaseDataSource] = []

    if config.goldapi_key:
        sources.append(GoldAPIDataSource(config.goldapi_key))

    sources.append(SinaDataSource())

    return FallbackDataSource(sources)


def create_all_sources(config: Settings | None = None) -> list[BaseDataSource]:
    """创建所有可用数据源（用于并行采集）"""
    config = config if config is not None else settings
    sources: list[BaseDataSource] = []

    if config.goldapi_key:
        sources.append(GoldAPIDataSource(config.goldapi_key))

    sources.append(SinaDataSource())

    return sources
