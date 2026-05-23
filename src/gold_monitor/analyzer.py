"""大模型分析模块 - 金价波动原因分析"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)

# 联网搜索时限定的可信财经来源（控制成本、提升质量）
_SEARCH_ALLOWED_DOMAINS = [
    "reuters.com", "kitco.com", "investing.com",
    "bloomberg.com", "marketwatch.com", "fxstreet.com",
]


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


@dataclass 
class SmartAnalysisReport:
    """智能分析报告（网络搜索版）"""
    title: str  # 标题
    market_overview: str  # 市场概况
    recent_trend: str  # 近期走势
    key_factors: list[str]  # 影响因素
    price_prediction: str  # 价格预测
    buy_timing: str  # 买入时机
    recommendation: str  # 操作建议
    risk_warning: str  # 风险提示
    generated_at: datetime
    raw_response: str
    web_search_used: bool = False  # 本次是否真正联网搜索
    sources: list = field(default_factory=list)  # 引用来源 [{url, title}]


class LLMProvider(ABC):
    """大模型提供商基类"""

    @abstractmethod
    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        """分析金价波动"""
        pass
    
    def supports_web_search(self) -> bool:
        """该提供商当前配置下是否支持真正的联网搜索"""
        return False

    async def smart_analyze(self) -> SmartAnalysisReport:
        """智能分析 - 优先真正联网搜索，不支持或失败时降级为无联网分析并明确标注"""
        prompt = self._build_smart_prompt()

        if self.supports_web_search():
            try:
                text, sources = await self._call_llm_with_search(prompt)
                if text and text.strip():
                    report = self._parse_smart_response(text)
                    report.web_search_used = True
                    report.sources = sources
                    return report
                logger.warning("联网搜索返回空内容，降级为无联网分析")
            except Exception as e:
                logger.warning("联网搜索分析失败，降级为无联网分析: %s", e)

        # 无联网（或降级）路径：明确标注，避免误导
        response = await self._call_llm(prompt)
        report = self._parse_smart_response(response)
        report.web_search_used = False
        report.risk_warning = (
            "⚠️ 本次分析未启用联网搜索，价格与市场数据可能不是最新，仅供参考。\n"
            + report.risk_warning
        )
        return report

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM（子类实现，无联网）"""
        raise NotImplementedError

    async def _call_llm_with_search(self, prompt: str) -> tuple[str, list]:
        """调用 LLM 并启用联网搜索（支持的子类实现）

        返回: (正文文本, 来源列表[{url, title}])
        """
        raise NotImplementedError
    
    def _build_smart_prompt(self) -> str:
        """构建智能分析提示词"""
        today = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
        return f"""你是一位专业的黄金市场分析师。今天是 {today}。

请你搜索并分析最近一周的国际黄金价格走势，给出专业的市场分析报告。

## 分析要求

请按以下格式输出分析报告：

### 📊 市场概况
简要描述当前黄金市场的整体情况，包括最新价格、本周涨跌幅等。

### 📈 近期走势
分析最近5-7天的金价走势，包括关键价位、支撑位、阻力位等。

### 🔍 影响因素
列出3-5个影响近期金价的主要因素，如：
- 美联储政策
- 美元走势
- 地缘政治
- 通胀数据
- 市场避险情绪等

### 🔮 价格预测
预测未来3-7天的金价走势范围，给出可能的价格区间。

### ⏰ 买入时机
分析当前是否适合买入黄金，如果不适合，什么价位适合入场。

### 💡 操作建议
给出具体的操作建议：
- 短线操作建议
- 中长线配置建议
- 仓位建议

### ⚠️ 风险提示
提醒投资者注意的风险因素。

请确保分析基于最新的市场数据，用中文回答，保持专业、客观的分析风格。"""

    def _parse_smart_response(self, response: str) -> SmartAnalysisReport:
        """解析智能分析响应"""
        sections = {
            "market_overview": "",
            "recent_trend": "",
            "key_factors": [],
            "price_prediction": "",
            "buy_timing": "",
            "recommendation": "",
            "risk_warning": ""
        }
        
        current_section = None
        current_content = []
        
        for line in response.split('\n'):
            line_lower = line.lower()
            
            # 检测章节标题
            if '市场概况' in line or 'market' in line_lower:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "market_overview"
                current_content = []
            elif '近期走势' in line or '走势' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "recent_trend"
                current_content = []
            elif '影响因素' in line or '因素' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "key_factors"
                current_content = []
            elif '价格预测' in line or '预测' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "price_prediction"
                current_content = []
            elif '买入时机' in line or '时机' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "buy_timing"
                current_content = []
            elif '操作建议' in line or '建议' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "recommendation"
                current_content = []
            elif '风险提示' in line or '风险' in line:
                if current_section and current_content:
                    self._save_section(sections, current_section, current_content)
                current_section = "risk_warning"
                current_content = []
            elif current_section:
                # 跳过标题行本身
                if not line.startswith('#'):
                    current_content.append(line)
        
        # 保存最后一个章节
        if current_section and current_content:
            self._save_section(sections, current_section, current_content)
        
        # 处理影响因素列表
        if isinstance(sections["key_factors"], str):
            factors = []
            for line in sections["key_factors"].split('\n'):
                line = line.strip()
                if line.startswith(('-', '•', '*', '·')) or (len(line) > 0 and line[0].isdigit()):
                    factor = line.lstrip('-•*·0123456789. ').strip()
                    if factor:
                        factors.append(factor)
            sections["key_factors"] = factors if factors else ["市场供需变化", "宏观经济影响", "地缘政治因素"]
        
        return SmartAnalysisReport(
            title=f"黄金市场分析报告 - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            market_overview=sections["market_overview"] or "暂无数据",
            recent_trend=sections["recent_trend"] or "暂无数据",
            key_factors=sections["key_factors"] if isinstance(sections["key_factors"], list) else ["暂无数据"],
            price_prediction=sections["price_prediction"] or "暂无预测",
            buy_timing=sections["buy_timing"] or "建议观望",
            recommendation=sections["recommendation"] or "建议谨慎操作",
            risk_warning=sections["risk_warning"] or "投资有风险，入市需谨慎",
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response=response
        )
    
    def _save_section(self, sections: dict, section: str, content: list):
        """保存章节内容"""
        text = '\n'.join(content).strip()
        if section == "key_factors":
            sections[section] = text  # 后续会解析为列表
        else:
            sections[section] = text

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
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response=response
        )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商"""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model or "claude-sonnet-4-20250514"
        if not self.api_key:
            raise ValueError("需要配置 Anthropic API Key")

    async def _call_llm(self, prompt: str) -> str:
        """调用 Anthropic API（无联网）"""
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        message = await client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text

    def supports_web_search(self) -> bool:
        return True

    async def _call_llm_with_search(self, prompt: str) -> tuple[str, list]:
        """调用 Anthropic 并启用官方 web_search 服务端工具"""
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=120.0)
        message = await client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
                "allowed_domains": _SEARCH_ALLOWED_DOMAINS,
            }],
        )
        # 联网模式下 content 是 block 列表（server_tool_use / web_search_tool_result / text）
        # 必须遍历取 text 块，不能再用 content[0].text
        text_parts: list[str] = []
        sources: list = []
        seen: set[str] = set()
        for block in message.content:
            if getattr(block, "type", None) != "text":
                continue
            text_parts.append(getattr(block, "text", "") or "")
            for c in (getattr(block, "citations", None) or []):
                url = getattr(c, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": getattr(c, "title", "") or ""})
        return "".join(text_parts), sources

    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        prompt = self._build_prompt(context)
        response_text = await self._call_llm(prompt)
        return self._parse_response(response_text)


class OpenAIProvider(LLMProvider):
    """OpenAI 提供商（兼容 API）"""

    # function-calling 联网循环最多轮数，防止模型反复调用搜索导致死循环
    MAX_TOOL_ROUNDS = 3

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        tavily_api_key: str | None = None,
    ):
        self.api_key = api_key or settings.openai_api_key
        self.base_url = self._normalize_base_url(base_url or settings.openai_base_url)
        # model 默认值修正：deepseek 端点未显式给 model 时用 deepseek-chat 兜底
        if model:
            self.model = model
        elif self.base_url and "deepseek" in self.base_url:
            self.model = "deepseek-chat"
        else:
            self.model = "gpt-4o-mini"
        # 读实例值而非每次读全局，保证测试隔离
        self._tavily_api_key = (
            tavily_api_key if tavily_api_key is not None else settings.tavily_api_key
        ) or ""
        if not self.api_key:
            raise ValueError("需要配置 OpenAI API Key")
    
    @staticmethod
    def _normalize_base_url(url: str | None) -> str | None:
        """标准化 base_url，自动添加 /v1"""
        if not url:
            return None
        url = url.rstrip('/')
        # 如果 URL 不包含 /v1，自动添加
        if not url.endswith('/v1') and '/v1' not in url:
            url = f"{url}/v1"
        return url

    async def _call_llm(self, prompt: str) -> str:
        """调用 OpenAI API（无联网）"""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一位专业的黄金市场分析师。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=2048
        )
        return response.choices[0].message.content

    def supports_web_search(self) -> bool:
        # 配了 Tavily → 任意兼容接口（含 DeepSeek）均可通过 function-calling 联网
        if self._tavily_api_key:
            return True
        # 否则仅 OpenAI 官方端点支持原生联网搜索；第三方兼容接口降级处理
        return not self.base_url or "api.openai.com" in self.base_url

    async def _call_llm_with_search(self, prompt: str) -> tuple[str, list]:
        """调用 LLM 并启用联网搜索。

        分流：
        - 官方 OpenAI 端点且未配 Tavily → 走原生 Responses API web_search（保持不变）。
        - 配了 Tavily（含 DeepSeek/兼容接口）→ 走 function-calling + Tavily 路径。
        """
        is_official = not self.base_url or "api.openai.com" in self.base_url
        if is_official and not self._tavily_api_key:
            return await self._search_via_responses_api(prompt)
        return await self._search_via_tavily_tools(prompt)

    async def _search_via_responses_api(self, prompt: str) -> tuple[str, list]:
        """调用 OpenAI Responses API 并启用原生 web_search 工具"""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0)
        response = await client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
        text = getattr(response, "output_text", "") or ""
        sources: list = []
        seen: set[str] = set()
        try:
            for item in (getattr(response, "output", None) or []):
                for content in (getattr(item, "content", None) or []):
                    for ann in (getattr(content, "annotations", None) or []):
                        if getattr(ann, "type", "") == "url_citation":
                            url = getattr(ann, "url", None)
                            if url and url not in seen:
                                seen.add(url)
                                sources.append({"url": url, "title": getattr(ann, "title", "") or ""})
        except Exception:
            pass
        return text, sources

    async def _search_via_tavily_tools(self, prompt: str) -> tuple[str, list]:
        """function-calling 联网循环：模型决定搜什么，我们用 Tavily 执行并回传结果。

        用非思考模式（不传 thinking/extra_body），规避 DeepSeek 思考模式下
        "需回传 reasoning_content 否则 400" 的复杂度。

        Tavily 调用或网络硬失败会向上抛，交由 smart_analyze 降级。
        """
        import json

        from openai import AsyncOpenAI

        from .web_search import tavily_search

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0)
        tools: list[Any] = [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜索最近一周的国际黄金市场/财经新闻与价格数据，返回带来源链接的结果",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，中英文均可"}
                    },
                    "required": ["query"],
                },
            },
        }]
        messages: list[Any] = [
            {"role": "system", "content": "你是专业黄金市场分析师，必要时调用 web_search 获取最新数据"},
            {"role": "user", "content": prompt},
        ]
        sources: list = []
        seen: set[str] = set()
        last_content = ""

        for _ in range(self.MAX_TOOL_ROUNDS):
            resp = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=2048,
                timeout=120.0,
            )
            msg = resp.choices[0].message
            last_content = msg.content or ""
            # 把助手消息追加进历史（含 tool_calls），供下一轮回传 tool 结果
            messages.append(msg.model_dump(exclude_none=True))

            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                return last_content, sources

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    query = args.get("query", "")
                except (ValueError, AttributeError, TypeError) as e:
                    logger.warning("解析 web_search 工具参数失败: %s", e)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "工具参数解析失败，请重试或直接基于已有信息回答。",
                    })
                    continue

                results = await tavily_search(
                    query,
                    api_key=self._tavily_api_key,
                    include_domains=_SEARCH_ALLOWED_DOMAINS,
                )
                tool_payload = []
                for r in results:
                    url = r.get("url")
                    title = r.get("title", "") or ""
                    if url and url not in seen:
                        seen.add(url)
                        sources.append({"url": url, "title": title})
                    tool_payload.append({
                        "url": url,
                        "title": title,
                        "content": r.get("content", "") or "",
                    })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_payload, ensure_ascii=False),
                })

        # 轮数用尽仍未收敛：再做一次不带工具的调用，让模型基于已搜集的搜索结果给出终稿，
        # 避免直接返回空内容被迫降级（白白浪费已执行的搜索）
        try:
            final = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=2048,
                timeout=120.0,
            )
            final_content = final.choices[0].message.content or ""
            if final_content.strip():
                return final_content, sources
        except Exception as e:
            logger.warning("联网搜索终稿调用失败，回退到最后一次内容: %s", e)

        return last_content, sources

    async def analyze(self, context: AnalysisContext) -> AnalysisReport:
        prompt = self._build_prompt(context)
        response_text = await self._call_llm(prompt)
        return self._parse_response(response_text)


class MockLLMProvider(LLMProvider):
    """模拟 LLM 提供商（用于测试）"""

    async def _call_llm(self, prompt: str) -> str:
        """模拟调用"""
        return "[Mock Response]"

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
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response="[Mock Response]"
        )
    
    async def smart_analyze(self) -> SmartAnalysisReport:
        """模拟智能分析"""
        return SmartAnalysisReport(
            title=f"黄金市场分析报告（模拟） - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            market_overview="【模拟数据】当前国际金价约 2650 美元/盎司，本周小幅震荡。",
            recent_trend="【模拟数据】近一周金价在 2620-2680 美元区间内震荡，整体维持高位运行。",
            key_factors=[
                "美联储货币政策预期",
                "美元指数走势",
                "地缘政治紧张局势",
                "全球央行购金需求",
                "通胀数据影响"
            ],
            price_prediction="【模拟数据】预计未来一周金价将在 2600-2700 美元区间波动。",
            buy_timing="【模拟数据】建议在金价回调至 2620 美元附近时考虑分批建仓。",
            recommendation="【模拟数据】短线建议观望，中长线可逢低配置。建议仓位控制在总资产的 5-10%。",
            risk_warning="此为模拟分析，请配置 AI 模型后获取真实分析。投资有风险，入市需谨慎。",
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response="[Mock Smart Analysis]"
        )


def create_llm_provider(provider: str | None = None) -> LLMProvider:
    """创建 LLM 提供商实例（优先使用动态配置）"""
    from .llm_config import get_llm_config_manager
    
    # 允许用环境变量强制指定（用于测试/部署时覆盖 Web 配置）
    # 例如 tests/test_web.py 会设置 GOLD_LLM_PROVIDER=mock
    if not provider:
        env_provider = (os.getenv("GOLD_LLM_PROVIDER") or "").strip().lower()
        if env_provider in {"mock", "anthropic", "openai"}:
            provider = env_provider
    
    # 每次都重新加载配置，确保使用最新设置
    config = get_llm_config_manager().reload_config()
    active_provider = config.get_active_provider()
    
    # 如果指定了 provider 参数，使用参数
    if provider:
        if provider == "mock":
            return MockLLMProvider()
        elif provider == "anthropic":
            return AnthropicProvider()
        elif provider == "openai":
            return OpenAIProvider()
        else:
            raise ValueError(f"不支持的 LLM 提供商: {provider}")
    
    # 使用当前激活的平台配置
    if active_provider:
        # Mock 模式
        if active_provider.id == "mock" or not active_provider.api_key:
            return MockLLMProvider()
        
        # 判断是否是 Anthropic（根据 URL 或名称）
        if "anthropic" in active_provider.base_url.lower() or "claude" in active_provider.name.lower():
            return AnthropicProvider(
                api_key=active_provider.api_key,
                model=config.active_model or None
            )
        
        # 其他都当作 OpenAI 兼容接口
        return OpenAIProvider(
            api_key=active_provider.api_key,
            base_url=active_provider.base_url or None,
            model=config.active_model or None
        )
    
    # 回退到 Mock
    return MockLLMProvider()


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
