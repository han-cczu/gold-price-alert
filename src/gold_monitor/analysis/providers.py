"""LLM adapters: external calls and search fallback behavior."""

import inspect
from contextlib import asynccontextmanager
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..config import Settings, settings as default_settings
from .types import AnalysisContext, AnalysisReport, SmartAnalysisReport
from .prompts import build_prompt, build_smart_prompt
from .parser import parse_response, parse_smart_response

logger = logging.getLogger(__name__)
_NO_SEARCH_WARNING = (
    "⚠️ 本次分析未启用联网搜索，价格与市场数据可能不是最新，仅供参考。\n"
)
_SEARCH_ALLOWED_DOMAINS = [
    "reuters.com",
    "kitco.com",
    "investing.com",
    "bloomberg.com",
    "marketwatch.com",
    "fxstreet.com",
]


@asynccontextmanager
async def managed_client(client):
    """Close SDK clients on success, failures, and task cancellation."""
    try:
        yield client
    finally:
        closer = getattr(client, "close", None) or getattr(client, "aclose", None)
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result


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
                    report.web_search_used = getattr(self, "_last_search_used", True)
                    report.sources = sources
                    if not report.web_search_used:
                        report.risk_warning = _NO_SEARCH_WARNING + report.risk_warning
                    return report
                logger.warning("联网搜索返回空内容，降级为无联网分析")
            except Exception as e:
                logger.warning("联网搜索分析失败，降级为无联网分析: %s", e)

        # 无联网（或降级）路径：明确标注，避免误导
        response = await self._call_llm(prompt)
        report = self._parse_smart_response(response)
        report.web_search_used = False
        report.risk_warning = _NO_SEARCH_WARNING + report.risk_warning
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
        return build_smart_prompt()

    def _build_prompt(self, context: AnalysisContext) -> str:
        return build_prompt(context)

    def _parse_smart_response(self, response: str) -> SmartAnalysisReport:
        return parse_smart_response(response)

    def _parse_response(self, response: str) -> AnalysisReport:
        return parse_response(response)

    @staticmethod
    def _first_text_block(blocks: list[Any]) -> str:
        return next(
            (
                block.text
                for block in blocks
                if isinstance(getattr(block, "text", None), str)
            ),
            "",
        )

    async def close(self) -> None:
        """Adapters close per-call clients; retained for service-owned implementations."""

    async def probe(self) -> str:
        """A minimal request used by the connection check."""
        return await self._call_llm("hi")


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        settings: Settings | None = None,
        base_url: str | None = None,
    ):
        app_settings = settings or default_settings
        self.api_key = app_settings.anthropic_api_key if api_key is None else api_key
        self.base_url = base_url or None
        self.model = model or "claude-sonnet-4-20250514"
        if not self.api_key:
            raise ValueError("需要配置 Anthropic API Key")

    async def _call_llm(self, prompt: str, *, max_tokens: int = 2048) -> str:
        """调用 Anthropic API（无联网）"""
        import anthropic

        async with managed_client(
            anthropic.AsyncAnthropic(
                api_key=self.api_key, base_url=self.base_url, timeout=30.0
            )
        ) as client:
            message = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._first_text_block(list(message.content))

    async def probe(self) -> str:
        return await self._call_llm("hi", max_tokens=50)

    def supports_web_search(self) -> bool:
        return True

    async def _call_llm_with_search(self, prompt: str) -> tuple[str, list]:
        """调用 Anthropic 并启用官方 web_search 服务端工具"""
        import anthropic

        async with managed_client(
            anthropic.AsyncAnthropic(
                api_key=self.api_key, base_url=self.base_url, timeout=120.0
            )
        ) as client:
            message = await client.messages.create(
                model=self.model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 5,
                        "allowed_domains": _SEARCH_ALLOWED_DOMAINS,
                    }
                ],
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
                for c in getattr(block, "citations", None) or []:
                    url = getattr(c, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        sources.append(
                            {"url": url, "title": getattr(c, "title", "") or ""}
                        )
            self._last_search_used = bool(sources) or any(
                getattr(block, "type", None) == "web_search_tool_result"
                and isinstance(getattr(block, "content", None), list)
                for block in message.content
            )
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
        *,
        settings: Settings | None = None,
    ):
        app_settings = settings or default_settings
        self.api_key = app_settings.openai_api_key if api_key is None else api_key
        self.base_url = self._normalize_base_url(
            app_settings.openai_base_url if base_url is None else base_url
        )
        # deepseek 端点未显式给 model 时用当前在售的 deepseek-v4-flash 兜底；
        # 旧别名 deepseek-chat / deepseek-reasoner 已于 2026-07-24 下线。
        if model:
            self.model = model
        elif self.base_url and "deepseek" in self.base_url:
            self.model = "deepseek-v4-flash"
        else:
            self.model = "gpt-4o-mini"
        # 读实例值而非每次读全局，保证测试隔离
        self._tavily_api_key = (
            tavily_api_key
            if tavily_api_key is not None
            else app_settings.tavily_api_key
        ) or ""
        if not self.api_key:
            raise ValueError("需要配置 OpenAI API Key")

    _VERSIONED_PATH = re.compile(r"/v\d+\w*(?:/|$)")

    @classmethod
    def _normalize_base_url(cls, url: str | None) -> str | None:
        """Append ``/v1`` only when the address carries no API version yet.

        Bare hosts and relay prefixes such as ``https://relay.example/openai``
        gain ``/v1``; addresses that already name a version, including
        ``/api/paas/v4`` or ``/v1beta/openai``, are left untouched.
        """
        if not url:
            return None
        url = url.rstrip("/")
        if not cls._VERSIONED_PATH.search(urlparse(url).path):
            url = f"{url}/v1"
        return url

    async def _call_llm(self, prompt: str, *, max_tokens: int = 2048) -> str:
        """调用 OpenAI API（无联网）"""
        from openai import AsyncOpenAI

        async with managed_client(
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30.0)
        ) as client:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一位专业的黄金市场分析师。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""

    async def probe(self) -> str:
        return await self._call_llm("hi", max_tokens=50)

    def supports_web_search(self) -> bool:
        # 配了 Tavily → 任意兼容接口（含 DeepSeek）均可通过 function-calling 联网
        if self._tavily_api_key:
            return True
        # 否则仅 OpenAI 官方端点支持原生联网搜索；第三方兼容接口降级处理
        return not self.base_url or urlparse(self.base_url).hostname == "api.openai.com"

    async def _call_llm_with_search(self, prompt: str) -> tuple[str, list]:
        """调用 LLM 并启用联网搜索。

        分流：
        - 官方 OpenAI 端点且未配 Tavily → 走原生 Responses API web_search（保持不变）。
        - 配了 Tavily（含 DeepSeek/兼容接口）→ 走 function-calling + Tavily 路径。
        """
        is_official = (
            not self.base_url or urlparse(self.base_url).hostname == "api.openai.com"
        )
        if is_official and not self._tavily_api_key:
            return await self._search_via_responses_api(prompt)
        return await self._search_via_tavily_tools(prompt)

    async def _search_via_responses_api(self, prompt: str) -> tuple[str, list]:
        """调用 OpenAI Responses API 并启用原生 web_search 工具"""
        from openai import AsyncOpenAI

        async with managed_client(
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0)
        ) as client:
            response = await client.responses.create(
                model=self.model,
                tools=[{"type": "web_search"}],
                input=prompt,
            )
            text = getattr(response, "output_text", "") or ""
            sources: list = []
            seen: set[str] = set()
            try:
                for item in getattr(response, "output", None) or []:
                    for content in getattr(item, "content", None) or []:
                        for ann in getattr(content, "annotations", None) or []:
                            if getattr(ann, "type", "") == "url_citation":
                                url = getattr(ann, "url", None)
                                if url and url not in seen:
                                    seen.add(url)
                                    sources.append(
                                        {
                                            "url": url,
                                            "title": getattr(ann, "title", "") or "",
                                        }
                                    )
            except Exception:
                pass
            self._last_search_used = bool(sources) or any(
                getattr(item, "type", None) == "web_search_call"
                and getattr(item, "status", "completed") == "completed"
                for item in getattr(response, "output", None) or []
            )
            return text, sources

    async def _search_via_tavily_tools(self, prompt: str) -> tuple[str, list]:
        """function-calling 联网循环：模型决定搜什么，我们用 Tavily 执行并回传结果。

        用非思考模式（不传 thinking/extra_body），规避 DeepSeek 思考模式下
        "需回传 reasoning_content 否则 400" 的复杂度。

        Tavily 调用或网络硬失败会向上抛，交由 smart_analyze 降级。
        """
        import json

        from openai import AsyncOpenAI

        from ..web_search import tavily_search

        self._last_search_used = False

        async with managed_client(
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0)
        ) as client:
            tools: list[Any] = [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "搜索最近一周的国际黄金市场/财经新闻与价格数据，返回带来源链接的结果",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "搜索关键词，中英文均可",
                                }
                            },
                            "required": ["query"],
                        },
                    },
                }
            ]
            messages: list[Any] = [
                {
                    "role": "system",
                    "content": "你是专业黄金市场分析师，必要时调用 web_search 获取最新数据",
                },
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
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "工具参数解析失败，请重试或直接基于已有信息回答。",
                            }
                        )
                        continue

                    results = await tavily_search(
                        query,
                        api_key=self._tavily_api_key,
                        include_domains=_SEARCH_ALLOWED_DOMAINS,
                    )
                    self._last_search_used = True
                    tool_payload = []
                    for r in results:
                        url = r.get("url")
                        title = r.get("title", "") or ""
                        if url and url not in seen:
                            seen.add(url)
                            sources.append({"url": url, "title": title})
                        tool_payload.append(
                            {
                                "url": url,
                                "title": title,
                                "content": r.get("content", "") or "",
                            }
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(tool_payload, ensure_ascii=False),
                        }
                    )

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
                "机构资金流向变化",
            ],
            market_sentiment="偏多" if context.price_change > 0 else "偏空",
            recommendation="建议关注关键支撑位，控制仓位风险",
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response="[Mock Response]",
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
                "通胀数据影响",
            ],
            price_prediction="【模拟数据】预计未来一周金价将在 2600-2700 美元区间波动。",
            buy_timing="【模拟数据】建议在金价回调至 2620 美元附近时考虑分批建仓。",
            recommendation="【模拟数据】短线建议观望，中长线可逢低配置。建议仓位控制在总资产的 5-10%。",
            risk_warning="此为模拟分析，请配置 AI 模型后获取真实分析。投资有风险，入市需谨慎。",
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            raw_response="[Mock Smart Analysis]",
        )
