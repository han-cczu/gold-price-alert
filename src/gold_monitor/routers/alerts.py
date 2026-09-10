"""Alert history HTTP adapter."""

from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from ..dependencies import get_runtime
from ..schemas import AlertResponse
from ..time_utils import utcnow

router = APIRouter()


@router.get("/api/alerts", response_model=list[AlertResponse])
async def get_alerts(
    limit: int = Query(50, ge=1, le=200),
    alert_type: Optional[str] = None,
    hours: Optional[int] = Query(None, ge=1, le=720),
    runtime=Depends(get_runtime),
):
    start = utcnow() - timedelta(hours=hours) if hours else None
    records = await runtime.db.run(
        runtime.db.get_alerts, limit=limit, alert_type=alert_type, start=start
    )
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
