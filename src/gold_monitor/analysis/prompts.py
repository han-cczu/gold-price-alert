"""Prompt construction without provider or application dependencies."""

from datetime import datetime, timezone
from .types import AnalysisContext


def build_smart_prompt() -> str:
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


def build_prompt(context: AnalysisContext) -> str:
    """构建分析提示词"""
    direction = "上涨" if context.price_change > 0 else "下跌"
    recent_data = "\n".join(
        [
            f"  - {t.strftime('%H:%M:%S')}: ${p:.2f}"
            for t, p in context.recent_prices[-10:]
        ]
    )

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
