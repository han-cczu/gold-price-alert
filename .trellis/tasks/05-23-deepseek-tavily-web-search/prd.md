# PRD: 为 DeepSeek 接入基于 Tavily 的真实联网搜索

- **Task**: 05-23-deepseek-tavily-web-search
- **Assignee**: diaohan
- **Priority**: P2
- **Created**: 2026-05-23
- **Branch (current)**: fix/security-and-correctness-review

## 1. 背景与硬事实（已联网核实，2026-05-23）

来源：DeepSeek 官方文档 https://api-docs.deepseek.com/

1. **DeepSeek 官方 API 没有原生 web_search 服务端工具**。`tools` 仅支持 `type: "function"`
   （官方原文 "Currently, only functions are supported as a tool."）。
   → OpenAI 的 `{"type":"web_search"}`、Anthropic 的 `web_search_20250305` 在 DeepSeek 上都不可用，传了会被拒。
2. 网页版的 `deepseek-v4-flash-search` 等"联网模型"只存在于逆向 chat.deepseek.com 的 userToken 通道，
   **非官方、不稳定、有 ToS 风险，明确不采用**。
3. **DeepSeek 支持 function calling（Tool Calls），OpenAI 兼容**，可用 `client.chat.completions.create(tools=[...])`。
4. 当前模型 ID：`deepseek-v4-flash` / `deepseek-v4-pro`；旧别名 `deepseek-chat` / `deepseek-reasoner`
   于 2026-07-24 退役（仍可用，分别路由到 v4-flash 非思考/思考模式）。

**结论**：让 DeepSeek "联网" 的唯一正路 = function-calling + 外部搜索 API。本任务用 **Tavily** 作为搜索后端。

## 2. 目标 / 非目标

### 目标
- 让 `smart_analyze()` 在使用 DeepSeek（及任何 OpenAI 兼容、支持 tool calls 的接口）时，能进行**真实联网搜索**并带回**可点击的来源引用**。
- 联网能力来自我们自己执行的 Tavily 搜索；DeepSeek 只负责"决定搜什么"和"综合成报告"。
- 失败时**优雅降级**为无联网分析，并在 `risk_warning` 明确标注（沿用现有降级语义）。
- **零回归**：不改变 Anthropic 路径与官方 OpenAI 端点的现有联网行为。

### 非目标
- 不接入 DeepSeek 网页版逆向 token。
- 不实现多搜索引擎可插拔抽象（本期只接 Tavily；预留清晰接缝即可）。
- 不改前端（`SmartAnalysisReport.sources / web_search_used` 已存在，web 层已消费）。

## 3. 设计

### 3.1 配置（src/gold_monitor/config.py）
新增 Settings 字段（env 前缀 `GOLD_`，走 `.env`，**不进 llm_config.json**）：
```python
tavily_api_key: str = Field(default="", description="Tavily 搜索 API Key（启用 DeepSeek/兼容接口的真实联网搜索）")
```
- `.env.example` 增加一行 `GOLD_TAVILY_API_KEY=`，并注释说明用途。
- **安全要求**：Tavily key 严禁写入 `llm_config.json`（该文件被 git 跟踪）。

### 3.2 依赖（pyproject.toml）
- dependencies 增加 `tavily-python>=0.5.0`（`AsyncTavilyClient` 自 0.3.4 起提供，取较新稳定下限；落地时按 PyPI 当前版本二次确认）。

### 3.3 Tavily 封装（建议新增 src/gold_monitor/web_search.py）
- 提供一个轻量异步搜索函数，避免在 analyzer 里直接耦合 SDK 细节，便于测试 mock：
```python
async def tavily_search(query: str, *, api_key: str, include_domains: list[str],
                        max_results: int = 5) -> list[dict]:
    """返回 [{url, title, content, score}]；失败抛异常由上层降级。"""
    from tavily import AsyncTavilyClient
    client = AsyncTavilyClient(api_key)
    resp = await client.search(
        query=query, topic="news", time_range="week",
        search_depth="advanced", max_results=max_results,
        include_domains=include_domains,
    )
    return resp.get("results", []) or []
```
- 模块顶层不要 import tavily（延迟到函数内），与 analyzer 现有 `import anthropic/openai` 延迟导入风格一致。

### 3.4 analyzer.py：OpenAIProvider 增强
1. `__init__` 捕获 Tavily 可用性（保证测试隔离 —— 读实例值而非每次读全局）：
   ```python
   self._tavily_api_key = (tavily_api_key if tavily_api_key is not None else settings.tavily_api_key) or ""
   ```
   （新增可选构造参数 `tavily_api_key: str | None = None`，默认从 settings 取。）
2. `supports_web_search()` 改为：
   ```python
   if self._tavily_api_key:
       return True  # 任意兼容接口 + Tavily → 可联网（function-calling）
   return not self.base_url or "api.openai.com" in self.base_url  # 官方端点原生联网
   ```
3. `_call_llm_with_search(prompt)` 分流：
   - **官方 OpenAI 端点且未配 Tavily** → 走现有 Responses API 原生路径（**保持不变**）。
   - **配了 Tavily**（含 DeepSeek/兼容接口，也含"官方+配了Tavily"）→ 走新的 `_search_via_tavily_tools(prompt)`。
4. 新增 `_search_via_tavily_tools(prompt) -> tuple[str, list]`：function-calling 循环
   - 工具声明（OpenAI 兼容 function）：
     ```python
     tools = [{"type": "function", "function": {
         "name": "web_search",
         "description": "搜索最近一周的国际黄金市场/财经新闻与价格数据，返回带来源链接的结果",
         "parameters": {"type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词，中英文均可"}},
            "required": ["query"]}}}]
     ```
   - messages 初始 = system("你是专业黄金市场分析师，必要时调用 web_search 获取最新数据") + user(prompt)
   - 循环最多 `MAX_TOOL_ROUNDS = 3` 轮：
     - `resp = await client.chat.completions.create(model=self.model, messages=messages, tools=tools, tool_choice="auto", max_tokens=2048, timeout=120.0)`
     - `msg = resp.choices[0].message`；把 msg 追加进 messages（含其 tool_calls）
     - 若 `msg.tool_calls`：逐个执行
        - `json.loads(tc.function.arguments)` 包 try/except（DeepSeek 文档明确警告参数可能非法 JSON）
        - 调 `tavily_search(query, api_key=self._tavily_api_key, include_domains=_SEARCH_ALLOWED_DOMAINS)`
        - 收集 sources：每个 result 取 `{url, title}`，按 url 去重
        - 追加 `{"role":"tool","tool_call_id": tc.id, "content": <结果摘要 JSON/文本>}`
        - 继续下一轮
     - 否则（无 tool_calls）：`return (msg.content or "", sources)`
   - 循环用尽仍无终稿：返回最后一次 `msg.content`（可能为空 → 上层按"空内容"降级）。
   - **关键**：用非思考模型/默认模式跑工具循环，避免 DeepSeek 思考模式下"需回传 reasoning_content 否则 400"的复杂度（不传 `extra_body` thinking 即可）。
5. `model` 默认值修正（相关正确性坑）：当 `base_url` 指向 deepseek 且未显式给 model 时，默认用 `deepseek-chat` 而非 `gpt-4o-mini`：
   ```python
   if model:
       self.model = model
   elif self.base_url and "deepseek" in self.base_url:
       self.model = "deepseek-chat"
   else:
       self.model = "gpt-4o-mini"
   ```
   （主路径仍期望用户在 UI / llm_config.json 设 active_model；此为兜底防呆。）

### 3.5 降级语义（不改 smart_analyze 主体）
- `smart_analyze()` 现有逻辑已满足：联网抛异常或返回空 → 走无联网 `_call_llm` 并在 `risk_warning` 前缀 "⚠️ 本次分析未启用联网搜索…"。Tavily/工具循环的硬失败必须**向上抛**以触发该降级，禁止裸 `except: pass` 吞掉。
- 异常按类型捕获并记日志（参考现有 `logger.warning("联网搜索分析失败，降级...: %s", e)`）。

## 4. 验收标准

1. 配置 `GOLD_TAVILY_API_KEY` + 选用 DeepSeek（base_url=api.deepseek.com，active_model=deepseek-chat）后，
   `smart_analyze()` 返回 `web_search_used=True` 且 `sources` 非空（真实 Tavily 来源）。
2. 未配 Tavily key 时，DeepSeek 路径 `supports_web_search()` 返回 False → 走无联网并标注（与现状一致，**现有测试 `test_openai_supports_web_search_only_for_official_endpoint` 仍须通过**）。
3. Tavily/工具循环失败 → 降级为无联网分析，`risk_warning` 含 "未启用联网搜索"，`sources == []`。
4. 官方 OpenAI 端点 + 未配 Tavily：行为与改动前完全一致（原生 Responses 路径）。
5. `ruff` / `mypy` 通过；新增测试与 `pytest` 全绿；测试**不得**发起真实网络请求（mock `AsyncOpenAI` 与 `tavily_search`/`AsyncTavilyClient`）。

## 5. 测试计划（tests/test_analyzer.py 续写，沿用现有风格）

- `test_openai_supports_web_search_with_tavily`：构造 `OpenAIProvider(api_key="k", base_url="https://api.deepseek.com", tavily_api_key="tvly-x")` → `supports_web_search() is True`。
- `test_tavily_tool_loop_returns_sources`：monkeypatch `AsyncOpenAI`（首轮返回带 1 个 web_search tool_call 的 message，次轮返回终稿 content）+ monkeypatch `tavily_search` 返回固定 results → 断言返回文本非空、sources 含被搜来的 url、Tavily 被调用一次。
- `test_tavily_tool_loop_degrades_on_search_error`：`tavily_search` 抛异常 → `_call_llm_with_search` 抛出 → 经 `smart_analyze` 降级，`web_search_used False` 且 risk_warning 含标注。
- 复用现有 `_SearchableProvider` 风格，避免真实 SDK 调用。
- 保留并确认现有 6 个 analyzer 测试不回归。

## 6. 风险 / 注意

- **成本/延迟**：联网后单次分析延迟升至 ~10-40s，Tavily advanced 每查 2 credit。前端"分析中"反馈已存在；可后续加当日缓存（本期不做）。
- **DeepSeek 工具参数可能非法 JSON** → `json.loads` 必须 try/except，失败时跳过该 tool_call 或回传错误 tool 消息，不可崩。
- **tavily-python 版本**：落地前 `pip show tavily-python` / PyPI 二次确认 `AsyncTavilyClient.search` 的 kw 兼容（`topic`/`time_range`/`include_domains`）。
- llm_config.json 已被 git 跟踪并含明文 key —— 本任务**不**把 Tavily key 放进去；密钥安全清理是另一独立事项。
