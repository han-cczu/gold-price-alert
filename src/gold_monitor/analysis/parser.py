"""Parse fixed report sections without making network calls."""

from datetime import datetime, timezone
import re
from .types import AnalysisReport, SmartAnalysisReport


_SMART_SECTIONS = {
    "市场概况": "market_overview",
    "market overview": "market_overview",
    "近期走势": "recent_trend",
    "影响因素": "key_factors",
    "价格预测": "price_prediction",
    "买入时机": "buy_timing",
    "操作建议": "recommendation",
    "风险提示": "risk_warning",
}


def parse_smart_response(response: str) -> SmartAnalysisReport:
    """Recognize headings, without treating prose containing keywords as headings."""
    sections: dict[str, list[str]] = {key: [] for key in _SMART_SECTIONS.values()}
    current = None
    for line in response.splitlines():
        # Accept markdown headings, bold headings, and plain exact section titles.
        title = line.strip().strip("#* ").rstrip("：:")
        title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", title).strip().lower()
        section = _SMART_SECTIONS.get(title)
        if section is not None:
            current = section
        elif current is not None:
            sections[current].append(line)

    def content(key: str, default: str) -> str:
        return "\n".join(sections[key]).strip() or default

    factors = [
        re.sub(r"^\s*(?:[-•*·]|\d+[.)、])\s*", "", line).strip()
        for line in sections["key_factors"]
        if line.strip()
    ]
    return SmartAnalysisReport(
        title=f"黄金市场分析报告 - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        market_overview=content("market_overview", "暂无数据"),
        recent_trend=content("recent_trend", "暂无数据"),
        key_factors=factors or ["暂无数据"],
        price_prediction=content("price_prediction", "暂无预测"),
        buy_timing=content("buy_timing", "建议观望"),
        recommendation=content("recommendation", "建议谨慎操作"),
        risk_warning=content("risk_warning", "投资有风险，入市需谨慎"),
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        raw_response=response,
    )


def parse_response(response: str) -> AnalysisReport:
    """Parse section headings without discarding recommendation prose or lists."""
    names = {
        "波动原因分析": "reasons",
        "原因分析": "reasons",
        "波动原因": "reasons",
        "市场情绪判断": "sentiment",
        "市场情绪": "sentiment",
        "短期展望": "outlook",
        "展望": "outlook",
        "操作建议": "recommendation",
    }
    headings = re.compile("(" + "|".join(names) + r")(?:[：:]\s*(.*))?")
    sections: dict[str, list[str]] = {name: [] for name in names.values()}
    current_section = None
    for line in response.splitlines():
        line = line.strip()
        if not line:
            continue
        title = line.replace("**", "").replace("__", "").strip("# ")
        title = re.sub(r"^(?:\d+[.)、]|[-•*])\s*", "", title)
        title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", title).strip()
        match = headings.fullmatch(title)
        if match is not None:
            current_section = names[match[1]]
            line = (match[2] or "").strip()
        if current_section is not None and line:
            sections[current_section].append(line)

    reasons = [
        re.sub(r"^(?:[-•*]|\d+[.)、])\s*", "", line).strip()
        for line in sections["reasons"]
    ]
    reasons = [reason for reason in reasons if reason]
    emotion = " ".join(sections["sentiment"])
    # Prefer explicit labels over incidental words such as "上涨乏力，偏空".
    if "偏空" in emotion:
        sentiment = "偏空"
    elif "偏多" in emotion:
        sentiment = "偏多"
    elif "震荡" in emotion:
        sentiment = "震荡"
    elif any(word in emotion for word in ("空", "跌", "悲观")):
        sentiment = "偏空"
    elif any(word in emotion for word in ("多", "涨", "乐观")):
        sentiment = "偏多"
    else:
        sentiment = "震荡"
    summary = f"金价{'上涨' if '多' in sentiment else '下跌' if '空' in sentiment else '震荡'}，市场情绪{sentiment}"
    recommendation = "\n".join(sections["recommendation"])

    return AnalysisReport(
        summary=summary,
        possible_reasons=reasons[:5] or ["市场正常波动", "短期供需变化"],
        market_sentiment=sentiment,
        recommendation=recommendation.strip() or "建议观望，等待更明确的市场信号",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        raw_response=response,
    )
