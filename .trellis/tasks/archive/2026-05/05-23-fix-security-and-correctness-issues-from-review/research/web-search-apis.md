# Research: 为黄金市场分析接入"真正的联网搜索"（Anthropic vs OpenAI）

- **Query**: 如何为 smart_analyze() 接入真正的联网搜索，对比 Anthropic 与 OpenAI 两条路径，给出最小可用示例、统一抽象与降级策略、成本/延迟注意点
- **Scope**: external（API 形态）+ internal（analyzer.py 现状）
- **Date**: 2026-05-23

> ⚠️ 可信度声明（必读）
> 研究子代理环境禁用了联网搜索（exa/web）与 Bash，无法在线核验 2026 年最新官方文档。
> 内容基于：(1) 本仓库 analyzer.py、pyproject.toml 的实读结果（可信）；(2) 训练知识（截止 2026-01）中的 API 形态（需上线前二次核验）。
> 凡涉及"工具类型字符串/模型名/SDK 最低版本"的，均标注 【需核验】。落地前对照官方页面：
> - Anthropic Web Search Tool: https://docs.anthropic.com/en/docs/build-with-claude/tool-use/web-search-tool
> - OpenAI Web Search / Responses: https://platform.openai.com/docs/guides/tools-web-search

## 现状（internal，已实读）

| File | 关键事实 |
|---|---|
| analyzer.py:301-310 | AnthropicProvider._call_llm 用 messages.create，未传 tools，纯模型生成（无联网）。返回 message.content[0].text。 |
| analyzer.py:339-351 | OpenAIProvider._call_llm 用 chat.completions.create，未启用 tool。 |
| analyzer.py:297 / :324 | 默认模型 "claude-sonnet-4-20250514" / "gpt-4o-mini"。 |
| analyzer.py:57-61 | smart_analyze() = _build_smart_prompt → _call_llm → _parse_smart_response。唯一改造点。 |
| analyzer.py:67-107 | 提示词写"请你搜索最近一周……"，但无联网能力 → 编造。这正是要修的"假联网"。 |
| analyzer.py:439-450 | provider 分流：base_url 含 anthropic 或名称含 claude → Anthropic；其余当 OpenAI 兼容接口（DeepSeek/通义走这里）。 |
| pyproject.toml:19-20 | 当前 anthropic>=0.18、openai>=1.10。 |

核心结论：当前两条路径都无联网，smart_analyze() 的"搜索最近一周金价"提示无效。

## 1) Anthropic Web Search 工具

- server-side tool：只在 messages.create 的 tools 声明，Claude 自动搜并带回，无需自己实现 executor。
- 工具声明：type "web_search_20250305"【需核验后缀】, name "web_search", 可选 max_uses / allowed_domains / user_location。
- 模型要求：近代模型（claude-sonnet-4-* 在范围内）【需核验】。
- 返回：message.content 是 block 列表（server_tool_use / web_search_tool_result / text）。**不能再用 content[0].text**（analyzer.py:310 在联网模式会出错）。text block 上挂 citations。
- 取正文：遍历 content 取 type=="text" 拼接。
- 计费：token 外按搜索次数计费（约 $10/1000 + 抓取 input token）【需核验价格】。

```python
import anthropic
async def anthropic_web_search(prompt, api_key, model):
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0)
    message = await client.messages.create(
        model=model, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    )
    text_parts, citations = [], []
    for block in message.content:   # 遍历，不能 content[0].text
        if block.type == "text":
            text_parts.append(block.text)
            for c in (getattr(block, "citations", None) or []):
                citations.append({"url": getattr(c,"url",None), "title": getattr(c,"title",None)})
    return "".join(text_parts), citations
```

SDK：anthropic>=0.18 过旧，需升级到约 >=0.40+（可能 >=0.49/0.50）【需核验】。**唯一明确需改 pyproject 的依赖**。

## 2) OpenAI 路径

(A) Responses API（推荐）：client.responses.create(tools=[{"type":"web_search"}])，取 resp.output_text；引用在 annotations(url_citation)。需 openai>=1.50【需核验】。
(B) 专用 search 模型（改动最小）：model="gpt-4o-search-preview"【需核验】，沿用 chat.completions，自动联网。
(C) 第三方兼容接口（DeepSeek/通义/自建）：**不支持，必须降级**——传 tools 多半 400。检测 base_url 非 api.openai.com → 不联网 + 明确标注。

## 3) 推荐工程方案：统一 smart_analyze() + 能力分流

- LLMProvider 加 `supports_web_search() -> bool`（默认 False）。
- SmartAnalysisReport 加 `sources: list[dict]` 与 `web_search_used: bool`。
- AnthropicProvider → True（升级 SDK 后）。
- OpenAIProvider → 仅 `not base_url or "api.openai.com" in base_url` 时 True，否则降级。
- 降级链（fail-soft）：联网失败 → 同 provider 无联网（标注"未联网"）→ MockLLMProvider.smart_analyze()。
- 超时：联网调用 timeout=120.0。异常按类型捕获（APIError/BadRequestError/TimeoutError）并记日志，不裸 except。
- 未联网时 risk_warning 前缀加"⚠️ 本次未启用联网搜索，数据可能过时"。

## 4) 成本/延迟

- 额外按搜索次数计费 + 抓取 input token（按普通调用 3-10x 估）。
- 延迟从 1-3s 升到 10-40s → 加大 timeout、前端"分析中"反馈、考虑当日缓存。
- allowed_domains 白名单（reuters/kitco/investing）控成本提质量。

## Caveats
- 所有 【需核验】 项（web_search_20250305、OpenAI 工具名、search-preview 模型名、SDK 最低版本、价格）**必须在 PR3 实现前用官方文档/web 核实**（主代理在 PR3 阶段执行）。
- analyzer.py:310 的 content[0].text 在联网模式会出错，需一并修。
