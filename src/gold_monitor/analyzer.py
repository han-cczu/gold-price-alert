"""Public analysis facade; existing imports remain supported."""

from datetime import datetime
from .analysis.types import AnalysisContext, AnalysisReport, SmartAnalysisReport
from .analysis.providers import (
    AnthropicProvider,
    OpenAIProvider,
    MockLLMProvider,
    LLMProvider,
)
from .analysis.factory import create_llm_provider

__all__ = [
    "AnalysisContext",
    "AnalysisReport",
    "SmartAnalysisReport",
    "AnthropicProvider",
    "OpenAIProvider",
    "MockLLMProvider",
    "LLMProvider",
    "GoldAnalyzer",
    "create_llm_provider",
]


class GoldAnalyzer:
    """金价分析器"""

    def __init__(self, llm_provider: LLMProvider | None = None):
        self._llm = llm_provider

    def _get_provider(self) -> LLMProvider:
        """延迟初始化 LLM 提供商"""
        if self._llm is None:
            try:
                self._llm = create_llm_provider()
            except ValueError:
                # 如果没有配置 API Key，使用 Mock
                self._llm = MockLLMProvider()
        return self._llm

    async def smart_analyze(self) -> SmartAnalysisReport:
        """智能分析 - AI 搜索网络数据进行分析"""
        provider = self._get_provider()
        return await provider.smart_analyze()

    async def analyze_volatility(
        self,
        current_price: float,
        price_change: float,
        recent_prices: list[tuple[datetime, float]],
        time_window_minutes: int = 5,
    ) -> AnalysisReport:
        """分析价格波动"""
        if current_price == 0:
            raise ValueError("当前价格不能为0")

        change_percent = (
            (price_change / (current_price - price_change)) * 100
            if price_change != current_price
            else 0
        )

        context = AnalysisContext(
            current_price=current_price,
            price_change=price_change,
            price_change_percent=change_percent,
            time_window_minutes=time_window_minutes,
            recent_prices=recent_prices,
        )

        provider = self._get_provider()
        return await provider.analyze(context)

    def format_report_markdown(self, report: AnalysisReport) -> str:
        """将分析报告格式化为 Markdown"""
        reasons_list = "\n".join([f"- {r}" for r in report.possible_reasons])

        return f"""# 金价波动分析报告

生成时间: {report.generated_at.strftime("%Y-%m-%d %H:%M:%S")}

## 摘要
{report.summary}

## 可能原因
{reasons_list}

## 市场情绪
{report.market_sentiment}

## 操作建议
{report.recommendation}

---
本报告由 AI 生成，仅供参考，不构成投资建议。
"""
