"""联网搜索后端 - 基于 Tavily

为 DeepSeek 等 OpenAI 兼容接口提供真实联网能力（DeepSeek 官方 API 无原生
web_search 工具，仅支持 function calling，故由我们自己执行搜索）。

SDK 延迟导入，避免在未配置 Tavily 时引入额外依赖加载成本，与 analyzer 中
anthropic/openai 的延迟导入风格一致。
"""

from typing import Any


async def tavily_search(
    query: str,
    *,
    api_key: str,
    include_domains: list[str],
    max_results: int = 5,
) -> list[dict]:
    """执行一次 Tavily 搜索。

    Args:
        query: 搜索关键词（中英文均可）。
        api_key: Tavily API Key。
        include_domains: 限定的可信来源域名列表。
        max_results: 返回结果数上限。

    Returns:
        结果列表，每项形如 ``{url, title, content, score}``。

    Raises:
        任何 Tavily / 网络错误都直接向上抛出，由上层负责降级处理。
    """
    from tavily import AsyncTavilyClient  # type: ignore[import-not-found,import-untyped]

    client = AsyncTavilyClient(api_key)
    resp: dict[str, Any] = await client.search(
        query=query,
        topic="news",
        time_range="week",
        search_depth="advanced",
        max_results=max_results,
        include_domains=include_domains,
    )
    return resp.get("results", []) or []
