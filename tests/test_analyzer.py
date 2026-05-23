"""大模型分析模块测试"""

import pytest
from datetime import datetime, timedelta, timezone

from gold_monitor.analyzer import (
    GoldAnalyzer, AnalysisReport, AnalysisContext,
    MockLLMProvider, create_llm_provider,
    LLMProvider, OpenAIProvider, SmartAnalysisReport,
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


class _SearchableProvider(LLMProvider):
    """测试用：可联网，按需模拟联网成功/失败"""

    def __init__(self, search_ok=True):
        self._search_ok = search_ok
        self.search_called = False
        self.plain_called = False

    def supports_web_search(self):
        return True

    async def _call_llm_with_search(self, prompt):
        self.search_called = True
        if not self._search_ok:
            raise RuntimeError("search boom")
        return (
            "### 市场概况\n金价上涨\n### 风险提示\n注意风险",
            [{"url": "https://reuters.com/x", "title": "R"}],
        )

    async def _call_llm(self, prompt):
        self.plain_called = True
        return "### 市场概况\n无联网\n### 风险提示\n基础风险"

    async def analyze(self, context):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_smart_analyze_uses_web_search_when_available():
    """支持联网且联网成功时，标注 web_search_used 并带回来源"""
    p = _SearchableProvider(search_ok=True)
    report = await p.smart_analyze()
    assert report.web_search_used is True
    assert p.search_called and not p.plain_called
    assert any("reuters" in s["url"] for s in report.sources)


@pytest.mark.asyncio
async def test_smart_analyze_degrades_when_search_fails():
    """联网失败时降级为无联网分析，并在风险提示中明确标注"""
    p = _SearchableProvider(search_ok=False)
    report = await p.smart_analyze()
    assert report.web_search_used is False
    assert p.search_called and p.plain_called
    assert "未启用联网搜索" in report.risk_warning
    assert report.sources == []


def test_openai_supports_web_search_only_for_official_endpoint():
    """第三方兼容接口不应启用联网搜索（未配 Tavily 时）。

    显式传 tavily_api_key="" 以隔离环境中的 GOLD_TAVILY_API_KEY，保证测试可重复。
    """
    official = OpenAIProvider(api_key="k", base_url="https://api.openai.com/v1", tavily_api_key="")
    assert official.supports_web_search() is True
    third_party = OpenAIProvider(api_key="k", base_url="https://api.deepseek.com", tavily_api_key="")
    assert third_party.supports_web_search() is False


def test_openai_supports_web_search_with_tavily():
    """配了 Tavily key 后，DeepSeek 兼容接口也支持联网搜索"""
    provider = OpenAIProvider(
        api_key="k",
        base_url="https://api.deepseek.com",
        tavily_api_key="tvly-x",
    )
    assert provider.supports_web_search() is True


def test_openai_deepseek_default_model():
    """deepseek 端点未显式给 model 时默认 deepseek-chat"""
    provider = OpenAIProvider(api_key="k", base_url="https://api.deepseek.com")
    assert provider.model == "deepseek-chat"
    official = OpenAIProvider(api_key="k", base_url="https://api.openai.com/v1")
    assert official.model == "gpt-4o-mini"


class _FakeFunction:
    def __init__(self, arguments):
        self.name = "web_search"
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, arguments):
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.role = "assistant"

    def model_dump(self, exclude_none=False):
        data = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        if exclude_none:
            data = {k: v for k, v in data.items() if v is not None}
        return data


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeCompletion:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeCompletion(self._messages.pop(0))


class _FakeChat:
    def __init__(self, messages):
        self.completions = _FakeCompletions(messages)


class _FakeAsyncOpenAI:
    """共享一个 chat 实例，便于断言调用次数"""
    _chat = None

    def __init__(self, *args, **kwargs):
        self.chat = _FakeAsyncOpenAI._chat


@pytest.mark.asyncio
async def test_tavily_tool_loop_returns_sources(monkeypatch):
    """首轮发起 web_search tool_call、次轮给终稿；返回正文非空且 sources 含被搜 url"""
    # 首轮：带一个 web_search tool_call；次轮：纯文本终稿
    messages = [
        _FakeMessage(content=None, tool_calls=[_FakeToolCall("call_1", '{"query": "gold price"}')]),
        _FakeMessage(content="### 市场概况\n金价上涨\n### 风险提示\n注意风险", tool_calls=None),
    ]
    _FakeAsyncOpenAI._chat = _FakeChat(messages)
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    # mock Tavily 搜索（不发真实请求）
    search_calls = []

    async def fake_tavily_search(query, *, api_key, include_domains, max_results=5):
        search_calls.append({"query": query, "api_key": api_key})
        return [
            {"url": "https://reuters.com/gold", "title": "Gold News", "content": "...", "score": 0.9},
        ]

    import gold_monitor.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "tavily_search", fake_tavily_search)

    provider = OpenAIProvider(
        api_key="k",
        base_url="https://api.deepseek.com",
        tavily_api_key="tvly-x",
    )
    text, sources = await provider._call_llm_with_search("prompt")

    assert text.strip()
    assert any("reuters.com/gold" in s["url"] for s in sources)
    assert len(search_calls) == 1
    assert search_calls[0]["api_key"] == "tvly-x"
    # 两轮 chat.completions.create
    assert len(_FakeAsyncOpenAI._chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_tavily_tool_loop_degrades_on_search_error(monkeypatch):
    """Tavily 抛错 → _call_llm_with_search 抛出 → 经 smart_analyze 降级并标注"""
    messages = [
        _FakeMessage(content=None, tool_calls=[_FakeToolCall("call_1", '{"query": "gold price"}')]),
    ]
    _FakeAsyncOpenAI._chat = _FakeChat(messages)
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    async def boom_tavily_search(query, *, api_key, include_domains, max_results=5):
        raise RuntimeError("tavily boom")

    import gold_monitor.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "tavily_search", boom_tavily_search)

    # 无联网降级路径需要 _call_llm，monkeypatch 掉避免真实请求
    async def fake_call_llm(self, prompt):
        return "### 市场概况\n无联网\n### 风险提示\n基础风险"

    monkeypatch.setattr(OpenAIProvider, "_call_llm", fake_call_llm)

    provider = OpenAIProvider(
        api_key="k",
        base_url="https://api.deepseek.com",
        tavily_api_key="tvly-x",
    )
    report = await provider.smart_analyze()

    assert report.web_search_used is False
    assert "未启用联网搜索" in report.risk_warning
    assert report.sources == []


@pytest.mark.asyncio
async def test_tavily_tool_loop_synthesizes_when_rounds_exhausted(monkeypatch):
    """模型每轮都搜索、用尽轮数后，应再做一次无工具终稿调用拿到正文"""
    tool_msgs = [
        _FakeMessage(content=None, tool_calls=[_FakeToolCall(f"c{i}", '{"query": "gold"}')])
        for i in range(OpenAIProvider.MAX_TOOL_ROUNDS)
    ]
    final_msg = _FakeMessage(content="### 市场概况\n基于搜索的终稿\n### 风险提示\n注意", tool_calls=None)
    _FakeAsyncOpenAI._chat = _FakeChat(tool_msgs + [final_msg])
    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    async def fake_tavily_search(query, *, api_key, include_domains, max_results=5):
        return [{"url": "https://kitco.com/g", "title": "G", "content": "c"}]

    import gold_monitor.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "tavily_search", fake_tavily_search)

    provider = OpenAIProvider(api_key="k", base_url="https://api.deepseek.com", tavily_api_key="tvly-x")
    text, sources = await provider._call_llm_with_search("prompt")

    assert "终稿" in text
    assert any("kitco.com" in s["url"] for s in sources)
    # MAX_TOOL_ROUNDS 轮工具调用 + 1 次无工具终稿
    assert len(_FakeAsyncOpenAI._chat.completions.calls) == OpenAIProvider.MAX_TOOL_ROUNDS + 1


@pytest.mark.asyncio
async def test_mock_provider_smart_analyze_marks_no_web_search():
    report = await MockLLMProvider().smart_analyze()
    assert isinstance(report, SmartAnalysisReport)
    assert report.web_search_used is False


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
