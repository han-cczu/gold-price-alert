"""数据源质量评分路由：/api/source-quality*"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import get_runtime

router = APIRouter()


@router.get("/api/source-quality")
async def get_source_quality(runtime=Depends(get_runtime)):
    """获取数据源质量评分"""
    collector = runtime.collector
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    qualities = collector.get_source_quality()
    confidence = collector.get_overall_confidence()

    return {
        "qualities": [q.to_dict() for q in qualities],
        "confidence": confidence,
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


@router.get("/api/source-quality/confidence")
async def get_data_confidence(runtime=Depends(get_runtime)):
    """获取整体数据置信度（轻量级接口，用于前端实时展示）"""
    collector = runtime.collector
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    return collector.get_overall_confidence()
