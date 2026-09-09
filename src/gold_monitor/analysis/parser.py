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
    """解析模型响应"""
    lines = response.strip().split("\n")

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
        elif line.startswith(("-", "•", "*", "1", "2", "3", "4", "5")):
            if current_section == "reasons":
                # 清理列表标记
                reason = line.lstrip("-•* 0123456789.").strip()
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
        raw_response=response,
    )
