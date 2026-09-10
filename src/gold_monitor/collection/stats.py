"""Collection statistics independent of network and persistence."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class SourceStats:
    """单个数据源统计"""

    name: str
    total_fetches: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0
    last_latency_ms: float = 0
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def success_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.success_count / self.total_fetches * 100

    @property
    def avg_latency_ms(self) -> float:
        if self.success_count == 0:
            return 0.0
        return self.total_latency_ms / self.success_count

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_fetches": self.total_fetches,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_latency_ms": round(self.last_latency_ms, 2),
            "last_success_at": self.last_success_at.isoformat()
            if self.last_success_at
            else None,
            "last_error": self.last_error,
        }


@dataclass
class SourceQuality:
    """数据源质量评分"""

    name: str
    reliability_score: float  # 可靠性评分 (0-100)
    freshness_score: float  # 数据新鲜度 (0-100)
    latency_score: float  # 延迟评分 (0-100)
    overall_score: float  # 综合评分 (0-100)
    weight: float  # 融合权重 (0-1)
    status: str  # healthy/degraded/unhealthy

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "reliability_score": round(self.reliability_score, 1),
            "freshness_score": round(self.freshness_score, 1),
            "latency_score": round(self.latency_score, 1),
            "overall_score": round(self.overall_score, 1),
            "weight": round(self.weight, 3),
            "status": self.status,
        }


def calculate_source_quality(stats: SourceStats) -> SourceQuality:
    """计算数据源质量评分"""
    # 可靠性评分（基于成功率）
    reliability_score = stats.success_rate

    # 新鲜度评分（基于最后成功时间）
    freshness_score = 100.0
    if stats.last_success_at:
        age_seconds = (utcnow() - stats.last_success_at).total_seconds()
        # 1分钟内满分，5分钟后开始衰减
        if age_seconds > 60:
            freshness_score = max(0, 100 - (age_seconds - 60) / 6)  # 每6秒减1分

    # 延迟评分（基于平均延迟）
    latency_score = 100.0
    if stats.avg_latency_ms > 0:
        # 500ms 以内满分，超过 5000ms 为 0 分
        latency_score = max(0, 100 - (stats.avg_latency_ms - 500) / 45)

    # 综合评分（加权平均）
    overall_score = (
        reliability_score * 0.5 + freshness_score * 0.3 + latency_score * 0.2
    )

    # 计算融合权重
    weight = overall_score / 100.0

    # 确定状态
    if overall_score >= 80:
        status = "healthy"
    elif overall_score >= 50:
        status = "degraded"
    else:
        status = "unhealthy"

    return SourceQuality(
        name=stats.name,
        reliability_score=reliability_score,
        freshness_score=freshness_score,
        latency_score=latency_score,
        overall_score=overall_score,
        weight=weight,
        status=status,
    )


@dataclass
class CollectorStats:
    """采集器统计信息"""

    started_at: datetime = field(default_factory=utcnow)
    total_fetches: int = 0
    success_count: int = 0
    failure_count: int = 0
    saved_count: int = 0
    skipped_count: int = 0
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    gaps_detected: int = 0
    gaps_filled: int = 0
    source_stats: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_fetches == 0:
            return 0.0
        return self.success_count / self.total_fetches * 100

    @property
    def uptime_seconds(self) -> float:
        return (utcnow() - self.started_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": round(self.uptime_seconds, 2),
            "total_fetches": self.total_fetches,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "saved_count": self.saved_count,
            "skipped_count": self.skipped_count,
            "success_rate": round(self.success_rate, 2),
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at.isoformat()
            if self.last_success_at
            else None,
            "last_failure_at": self.last_failure_at.isoformat()
            if self.last_failure_at
            else None,
            "last_error": self.last_error,
            "gaps_detected": self.gaps_detected,
            "gaps_filled": self.gaps_filled,
            "source_stats": {k: v.to_dict() for k, v in self.source_stats.items()},
        }
