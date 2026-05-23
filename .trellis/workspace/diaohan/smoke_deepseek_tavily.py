"""真实联网冒烟测试：DeepSeek + Tavily（function-calling 联网搜索）

自适应：
- 配了 GOLD_TAVILY_API_KEY → 跑完整联网（web_search 工具循环 + 来源）
- 未配 Tavily → 仅真实验证 DeepSeek 连通性 + 无联网降级路径

从 .env 读取 GOLD_OPENAI_API_KEY / GOLD_OPENAI_BASE_URL / GOLD_TAVILY_API_KEY。
不打印任何密钥。运行：
  PYTHONUTF8=1 python .trellis/workspace/diaohan/smoke_deepseek_tavily.py
"""

import asyncio
import sys

from gold_monitor.config import settings
from gold_monitor.analyzer import OpenAIProvider


async def _test_connectivity(provider: OpenAIProvider) -> bool:
    """真实调用 DeepSeek 一次，验证 key/模型可用。"""
    print("\n→ [连通性] 调用 DeepSeek（无联网）...")
    try:
        text = await provider._call_llm("用不超过10个字回答：你是什么模型？")
    except Exception as e:
        print(f"✗ DeepSeek 调用失败: {type(e).__name__}: {e}")
        return False
    print(f"✓ DeepSeek 响应: {text!r}")
    return True


async def _test_downgrade(provider: OpenAIProvider) -> bool:
    """未配 Tavily 时，smart_analyze 应真实跑 DeepSeek 并标注未联网。"""
    print("\n→ [降级路径] smart_analyze()（无 Tavily，应降级为无联网并标注）...")
    report = await provider.smart_analyze()
    print(f"  web_search_used = {report.web_search_used}")
    print(f"  sources         = {len(report.sources)}")
    print(f"  风险提示(前80字): {report.risk_warning[:80]}")
    ok = (report.web_search_used is False) and ("未启用联网搜索" in report.risk_warning)
    print("✓ 降级路径正确（已标注未联网）" if ok else "✗ 降级标注异常")
    return ok


async def _test_web_search(provider: OpenAIProvider) -> bool:
    """配了 Tavily 时，真实联网并带回来源。"""
    print("\n→ [联网] smart_analyze()（真实 web_search，可能 10-40s）...")
    report = await provider.smart_analyze()
    print(f"  web_search_used = {report.web_search_used}")
    print(f"  sources({len(report.sources)}):")
    for s in report.sources[:8]:
        print(f"    - {s.get('title') or '(无标题)'} | {s.get('url')}")
    print(f"  市场概况(前200字): {report.market_overview[:200]}")
    ok = report.web_search_used and bool(report.sources)
    print("✅ 联网生效且带回来源" if ok else "⚠️ 联网未生效（已降级），检查 Tavily key/额度或工具调用")
    return ok


async def main() -> int:
    if not settings.openai_api_key or "deepseek" not in (settings.openai_base_url or ""):
        print("✗ 缺 GOLD_OPENAI_API_KEY 或 GOLD_OPENAI_BASE_URL（应指向 deepseek）")
        return 2

    provider = OpenAIProvider()  # 从 settings 读 key/base_url/tavily_api_key
    has_tavily = bool(provider._tavily_api_key)
    print(f"model               = {provider.model}")
    print(f"base_url            = {provider.base_url}")
    print(f"tavily_key_set      = {has_tavily}")
    print(f"supports_web_search = {provider.supports_web_search()}")

    if not await _test_connectivity(provider):
        print("\n→ DeepSeek 连通性失败，先解决 key/模型再继续。")
        return 1

    if has_tavily:
        assert provider.supports_web_search(), "配了 Tavily 仍判定不支持联网，逻辑有误"
        ok = await _test_web_search(provider)
    else:
        print("\n（未配 Tavily，跳过真实联网，仅测降级路径；填 GOLD_TAVILY_API_KEY 后重跑可测联网）")
        ok = await _test_downgrade(provider)

    print("\n===== 冒烟结论 =====")
    print("✅ 通过" if ok else "⚠️ 见上方说明")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
