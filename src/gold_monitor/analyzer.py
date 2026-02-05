"""大模型分析模块 - 金价波动原因分析"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import settings


@dataclass
class AnalysisContext:
    """分析上下文"""
    current_price: float
    price_change: float
    price_change_percent: float
    time_window_minutes: int
    recent_prices: list[tuple[datetime, float]]
    additional_info: dict[str, Any] | None = None


@dataclass
class AnalysisReport:
    """分析报告"""
    summary: str
    possible_reasons: list[str]
    market_sentiment: str
    recommendation: str
    generated_at: datetime
    raw_response: str


class LLMProvider(ABC):
    """大模型提供商基类"""

    @abstractmethod
    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        """分析金价波动"""
        pass

    def _build_prompt(self, context: AnalysisContext) -> str:
        """构建分析提示词"""
        direction = "上涨" if context.price_change > 0 else "下跌"
        recent_data = "\n".join([
            f"  - {t.strftime('%H:%M:%S')}: ${p:.2f}"
            for t, p in context.recent_prices[-10:]
        ])

        prompt = f"""你是一位专业的黄金市场分析师。请分析以下金价波动情况并给出专业见解。

## 当前市场数据
- 当前金价: ${context.current_price:.2f} USD/盎司
- 价格变动: {direction} ${abs(context.price_change):.2f} ({context.price_change_percent:+.2f}%)
- 时间窗口: 最近 {context.time_window_minutes} 分钟

## 近期价格走势
{recent_data}

请从以下几个方面进行分析：

1. **波动原因分析**: 列出可能导致此次价格波动的 3-5 个主要原因
2. **市场情绪判断**: 当前市场是偏多、偏空还是震荡
3. **短期展望**: 对未来几小时的价格走势预判
4. **操作建议**: 给出简要的投资建议

请用中文回答，保持专业、客观的分析风格。"""

        return prompt

    def _parse_response(self, response: str) -> AnalysisReport:
        """解析模型响应"""
        lines = response.strip().split('\n')

        # 简单解析，提取关键信息
        summary = ""
        reasons = []
        sentiment = "震荡"
        recommendation = ""

        current_section = None
        for line in lines:
            line = line.strip()
            if not line:
                continue

            if "波动原因" in line or "原因分析" in line:
                current_section = "reasons"
            elif "市场情绪" in line:
                current_section = "sentiment"
            elif "短期展望" in line or "展望" in line:
                current_section = "outlook"
            elif "操作建议" in line or "建议" in line:
                current_section = "recommendation"
            elif line.startswith(('-', '•', '*', '1', '2', '3', '4', '5')):
                if current_section == "reasons":
                    # 清理列表标记
                    reason = line.lstrip('-•* 0123456789.').strip()
                    if reason:
                        reasons.append(reason)
            else:
                if current_section == "sentiment":
                    if "多" in line or "涨" in line or "乐观" in line:
                        sentiment = "偏多"
                    elif "空" in line or "跌" in line or "悲观" in line:
                        sentiment = "偏空"
                    else:
                        sentiment = "震荡"
                elif current_section == "recommendation":
                    recommendation += line + " "

        # 如果没有解析到原因，使用默认
        if not reasons:
            reasons = ["市场正常波动", "短期供需变化"]

        # 生成摘要
        if not summary:
            summary = f"金价{'上涨' if '多' in sentiment else '下跌' if '空' in sentiment else '震荡'}，市场情绪{sentiment}"

        return AnalysisReport(
            summary=summary,
            possible_reasons=reasons[:5],
            market_sentiment=sentiment,
            recommendation=recommendation.strip() or "建议观望，等待更明确的市场信号",
            generated_at=datetime.utcnow(),
            raw_response=response
        )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商"""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.anthropic_api_key
        if not self.api_key:
            raise ValueError("需要配置 Anthropic API Key")

    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        prompt = self._build_prompt(context)

        message = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        response_text = message.content[0].text
        return self._parse_response(response_text)


class OpenAIProvider(LLMProvider):
    """OpenAI 提供商（兼容 API）"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url or None
        if not self.api_key:
            raise ValueError("需要配置 OpenAI API Key")

    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        prompt = self._build_prompt(context)

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你是一位专业的黄金市场分析师。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1024
        )

        response_text = response.choices[0].message.content
        return self._parse_response(response_text)


class MockLLMProvider(LLMProvider):
    """模拟 LLM 提供商（用于测试）"""

    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        direction = "上涨" if context.price_change > 0 else "下跌"

        return AnalysisReport(
            summary=f"金价短期{direction}，市场情绪偏{'多' if context.price_change > 0 else '空'}",
            possible_reasons=[
                "美元指数波动影响",
                "地缘政治风险变化",
                "市场避险情绪调整",
                "技术面支撑/阻力位触发",
                "机构资金流向变化"
            ],
            market_sentiment="偏多" if context.price_change > 0 else "偏空",
            recommendation="建议关注关键支撑位，控制仓位风险",
            generated_at=datetime.utcnow(),
            raw_response="[Mock Response]"
        )


def create_llm_provider(provider: str | None = None) -> LLMProvider:
    """创建 LLM 提供商实例"""
    provider = provider or settings.llm_provider

    if provider == "anthropic":
        return AnthropicProvider()
    elif provider == "openai":
        return OpenAIProvider()
    elif provider == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"不支持的 LLM 提供商: {provider}")


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

    async def analyze_volatility(
        self,
        current_price: float,
        price_change: float,
        recent_prices: list[tuple[datetime, float]],
        time_window_minutes: int = 5
    ) -> AnalysisReport:
        """分析价格波动"""
        if current_price == 0:
            raise ValueError("当前价格不能为0")

        change_percent = (price_change / (current_price - price_change)) * 100 if price_change != current_price else 0

        context = AnalysisContext(
            current_price=current_price,
            price_change=price_change,
            price_change_percent=change_percent,
            time_window_minutes=time_window_minutes,
            recent_prices=recent_prices
        )

        provider = self._get_provider()
        return await provider.analyze(context)

    def format_report_markdown(self, report: AnalysisReport) -> str:
        """将分析报告格式化为 Markdown"""
        reasons_list = "\n".join([f"- {r}" for r in report.possible_reasons])

        return f"""# 金价波动分析报告

生成时间: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}

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
