"""Pure volatility rules, independent of storage and transport."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import statistics


@dataclass
class VolatilityResult:
    """波动分析结果"""

    change_percent: float  # 首尾涨跌幅
    amplitude_percent: float  # 振幅 (high-low)/base
    rolling_std: float  # 滚动标准差
    consecutive_trend: int  # 连续同向次数
    event_type: str


class VolatilityDetector:
    """波动检测器 - 多维度分析，降低噪声误报"""

    def __init__(
        self,
        threshold_percent: float = 1.0,
        min_consecutive: int = 3,
        max_amplitude: float = 5.0,
    ):
        self._threshold = threshold_percent
        self._min_consecutive = min_consecutive
        self._max_amplitude = max_amplitude

    def analyze(
        self, prices: list[tuple[datetime, float]]
    ) -> Optional[VolatilityResult]:
        """分析价格序列，返回波动结果"""
        if len(prices) < 2:
            return None

        price_values = [p for _, p in prices]
        base_price = price_values[0]
        current_price = price_values[-1]

        if base_price == 0:
            return None

        # 1. 计算首尾涨跌幅
        change_percent = ((current_price - base_price) / base_price) * 100

        # 2. 计算振幅
        high = max(price_values)
        low = min(price_values)
        amplitude_percent = ((high - low) / base_price) * 100

        # 3. 计算滚动标准差
        rolling_std = 0.0
        if len(price_values) >= 3:
            rolling_std = statistics.stdev(price_values)

        # 4. 计算连续同向趋势
        consecutive_trend = self._calc_consecutive_trend(price_values)

        # 5. 检测事件类型
        event_type = self._detect_event(price_values, change_percent)

        return VolatilityResult(
            change_percent=change_percent,
            amplitude_percent=amplitude_percent,
            rolling_std=rolling_std,
            consecutive_trend=consecutive_trend,
            event_type=event_type,
        )

    def _calc_consecutive_trend(self, prices: list[float]) -> int:
        """计算连续同向趋势次数"""
        if len(prices) < 2:
            return 0

        consecutive = 1
        last_direction = None

        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff == 0:
                continue

            current_direction = 1 if diff > 0 else -1

            if last_direction is None:
                last_direction = current_direction
                consecutive = 1
            elif current_direction == last_direction:
                consecutive += 1
            else:
                last_direction = current_direction
                consecutive = 1

        return consecutive

    def _detect_event(self, prices: list[float], change_percent: float) -> str:
        """检测事件类型"""
        if len(prices) < 3:
            return "normal"

        # 检测是否为突破（最后几个价格连续创新高/新低）
        recent = prices[-3:]
        if (
            all(recent[i] > recent[i - 1] for i in range(1, len(recent)))
            and change_percent > 0
        ):
            return "breakout_up"
        if (
            all(recent[i] < recent[i - 1] for i in range(1, len(recent)))
            and change_percent < 0
        ):
            return "breakout_down"

        # 检测回落（先涨后跌或先跌后涨）
        mid_idx = len(prices) // 2
        first_half = prices[:mid_idx]
        second_half = prices[mid_idx:]

        if first_half and second_half:
            first_trend = sum(
                1
                for i in range(1, len(first_half))
                if first_half[i] > first_half[i - 1]
            )
            second_trend = sum(
                1
                for i in range(1, len(second_half))
                if second_half[i] > second_half[i - 1]
            )

            # 前半段涨、后半段跌，或者反过来
            if (
                first_trend > len(first_half) // 2
                and second_trend < len(second_half) // 2
            ):
                return "pullback"
            if (
                first_trend < len(first_half) // 2
                and second_trend > len(second_half) // 2
            ):
                return "pullback"

        return "normal"

    def should_alert(self, result: VolatilityResult) -> bool:
        """多条件联合判断，降低误报"""
        if result is None:
            return False

        # 基本条件：涨跌幅超过阈值
        if abs(result.change_percent) < self._threshold:
            return False

        # 抗噪条件1：至少连续N次采样确认趋势
        if result.consecutive_trend < self._min_consecutive:
            return False

        # 抗噪条件2：排除剧烈震荡（振幅过大说明是噪声）
        if result.amplitude_percent > self._max_amplitude:
            return False

        return True
