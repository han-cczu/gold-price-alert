"""告警相关路由：/api/alerts"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query

from ..schemas import AlertResponse
from ..state import db

router = APIRouter()


@router.get("/api/alerts", response_model=list[AlertResponse])
async def get_alerts(
    limit: int = Query(default=50, ge=1, le=200, description="最大记录数"),
    alert_type: Optional[str] = Query(
        default=None,
        description="告警类型过滤: threshold_upper, threshold_lower, volatility",
    ),
    hours: Optional[int] = Query(
        default=None, ge=1, le=720, description="时间范围（小时）"
    ),
):
    """获取告警历史（支持按类型和时间过滤）"""
    from ..models import AlertRecord

    with db.get_session() as session:
        query = session.query(AlertRecord)

        # 按类型过滤
        if alert_type:
            query = query.filter(AlertRecord.alert_type == alert_type)

        # 按时间范围过滤
        if hours:
            start_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                hours=hours
            )
            query = query.filter(AlertRecord.triggered_at >= start_time)

        records = query.order_by(AlertRecord.triggered_at.desc()).limit(limit).all()

        return [
            AlertResponse(
                id=r.id,
                alert_type=r.alert_type,
                price=r.price,
                message=r.message,
                triggered_at=r.triggered_at,
            )
            for r in records
        ]
