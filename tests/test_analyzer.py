"""大模型分析模块测试"""

import pytest
from datetime import datetime, timedelta, timezone

from gold_monitor.analyzer import (
    GoldAnalyzer, AnalysisReport, AnalysisContext,
    MockLLMProvider, create_llm_provider
)


@pytest.fixture
def analyzer():
    """创建使用 Mock 提供商的分析器"""
    return GoldAnalyzer(llm_provider=MockLLMProvider())


@pytest.fixture
def sample_prices():
    """生成示例价格数据"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prices = []
    base_price = 2000.0
    for i in range(10):
        timestamp = now - timedelta(minutes=10 - i)
        price = base_price + (i * 2)  # 价格逐渐上涨
        prices.append((timestamp, price))
    return prices


@pytest.mark.asyncio
async def test_analyze_volatility_upward(analyzer, sample_prices):
    """测试上涨趋势分析"""
    current_price = sample_prices[-1][1]
    first_price = sample_prices[0][1]
    price_change = current_price - first_price

    report = await analyzer.analyze_volatility(
        current_price=current_price,
        price_change=price_change,
        recent_prices=sample_prices,
        time_window_minutes=10
    )

    assert isinstance(report, AnalysisReport)
    assert report.summary is not None
    assert len(report.possible_reasons) > 0
    assert report.market_sentiment in ["偏多", "偏空", "震荡"]
    assert report.recommendation is not None
    assert report.generated_at is not None


@pytest.mark.asyncio
async def test_analyze_volatility_downward(analyzer):
    """测试下跌趋势分析"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prices = []
    base_price = 2050.0
    for i in range(10):
        timestamp = now - timedelta(minutes=10 - i)
        price = base_price - (i * 3)  # 价格逐渐下跌
        prices.append((timestamp, price))

    current_price = prices[-1][1]
    first_price = prices[0][1]
    price_change = current_price - first_price

    report = await analyzer.analyze_volatility(
        current_price=current_price,
        price_change=price_change,
        recent_prices=prices,
        time_window_minutes=10
    )

    assert report.market_sentiment in ["偏多", "偏空", "震荡"]


@pytest.mark.asyncio
async def test_analyze_with_zero_price():
    """测试零价格异常处理"""
    analyzer = GoldAnalyzer(llm_provider=MockLLMProvider())

    with pytest.raises(ValueError, match="当前价格不能为0"):
        await analyzer.analyze_volatility(
            current_price=0,
            price_change=0,
            recent_prices=[],
            time_window_minutes=5
        )


def test_format_report_markdown(analyzer):
    """测试 Markdown 报告生成"""
    report = AnalysisReport(
        summary="金价短期上涨，市场情绪偏多",
        possible_reasons=[
            "美元指数走弱",
            "地缘政治风险上升",
            "通胀预期增强"
        ],
        market_sentiment="偏多",
        recommendation="建议逢低买入",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        raw_response="[Raw Response]"
    )

    markdown = analyzer.format_report_markdown(report)

    assert "# 金价波动分析报告" in markdown
    assert "金价短期上涨" in markdown
    assert "美元指数走弱" in markdown
    assert "偏多" in markdown
    assert "逢低买入" in markdown


def test_create_mock_provider():
    """测试创建 Mock 提供商"""
    provider = create_llm_provider("mock")
    assert isinstance(provider, MockLLMProvider)


def test_create_invalid_provider():
    """测试创建无效提供商"""
    with pytest.raises(ValueError, match="不支持的 LLM 提供商"):
        create_llm_provider("invalid_provider")


@pytest.mark.asyncio
async def test_mock_provider_response():
    """测试 Mock 提供商响应"""
    provider = MockLLMProvider()

    context = AnalysisContext(
        current_price=2050.0,
        price_change=20.0,
        price_change_percent=1.0,
        time_window_minutes=5,
        recent_prices=[
            (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5), 2030.0),
            (datetime.now(timezone.utc).replace(tzinfo=None), 2050.0)
        ]
    )

    report = await provider.analyze(context)

    assert report.summary is not None
    assert "上涨" in report.summary  # price_change > 0
    assert len(report.possible_reasons) == 5
    assert report.raw_response == "[Mock Response]"
