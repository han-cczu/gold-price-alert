"""分析相关路由：/api/analysis、/api/smart-analysis、/api/analysis/history*"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..analyzer import GoldAnalyzer
from ..config import settings
from ..schemas import AnalysisResponse, SmartAnalysisResponse, RefreshAnalysisRequest
from ..state import db, get_smart_analysis_cache, _run_smart_analysis

router = APIRouter()


@router.get("/api/analysis", response_model=AnalysisResponse)
async def run_analysis():
    """运行 AI 分析（基于本地数据）"""
    # 优先使用“波动窗口”内的数据，避免窗口描述与实际取数跨度不一致
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(minutes=settings.alert_volatility_window)
    window_records = db.get_prices_in_range(start_time, end_time)

    if len(window_records) >= 2:
        records = window_records  # 时间升序
        time_window_minutes = settings.alert_volatility_window
    else:
        # 回退：使用最近 N 条数据，但用真实时间跨度作为窗口描述
        recent_desc = db.get_recent_prices(limit=20)  # 时间降序
        if len(recent_desc) < 2:
            raise HTTPException(status_code=400, detail="数据不足，无法分析")

        records = list(reversed(recent_desc))  # 时间升序
        span_seconds = (records[-1].timestamp - records[0].timestamp).total_seconds()
        time_window_minutes = max(1, int(span_seconds / 60))

    current_price = records[-1].price
    oldest_price = records[0].price
    price_change = current_price - oldest_price

    recent_prices = [(r.timestamp, r.price) for r in records]

    analyzer = GoldAnalyzer()
    report = await analyzer.analyze_volatility(
        current_price=current_price,
        price_change=price_change,
        recent_prices=recent_prices,
        time_window_minutes=time_window_minutes,
    )

    return AnalysisResponse(
        summary=report.summary,
        possible_reasons=report.possible_reasons,
        market_sentiment=report.market_sentiment,
        recommendation=report.recommendation,
        generated_at=report.generated_at,
    )


@router.get("/api/smart-analysis", response_model=SmartAnalysisResponse)
async def get_smart_analysis():
    """获取智能分析结果（使用缓存）"""
    cache = get_smart_analysis_cache()

    if cache:
        # 计算缓存年龄
        cache_age = (
            datetime.now(timezone.utc).replace(tzinfo=None) - cache["generated_at"]
        )
        cache_age_minutes = int(cache_age.total_seconds() / 60)

        return SmartAnalysisResponse(
            **cache, is_cached=True, cache_age_minutes=cache_age_minutes
        )

    # 没有缓存，执行分析
    result = await _run_smart_analysis()
    return SmartAnalysisResponse(**result, is_cached=False, cache_age_minutes=0)


@router.post("/api/smart-analysis/refresh")
async def refresh_smart_analysis(request: Optional[RefreshAnalysisRequest] = None):
    """手动刷新智能分析"""
    model = request.model if request else None
    try:
        result = await _run_smart_analysis(model=model)
        return {
            "success": True,
            "message": "分析已刷新",
            "data": result,  # 包含完整数据，包括 raw_response
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")


@router.get("/api/analysis/history")
async def get_analysis_history(
    limit: int = Query(default=50, le=200),
    analysis_type: Optional[str] = Query(
        default=None, description="分析类型: volatility, smart"
    ),
):
    """获取分析历史记录"""
    records = db.get_analysis_records(limit=limit, analysis_type=analysis_type)
    return {
        "records": [
            {
                "id": r.id,
                "analysis_type": r.analysis_type,
                "model_provider": r.model_provider,
                "model_name": r.model_name,
                "price_range_start": r.price_range_start.isoformat()
                if r.price_range_start
                else None,
                "price_range_end": r.price_range_end.isoformat()
                if r.price_range_end
                else None,
                "input_summary": r.input_summary,
                "result": r.get_result(),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
        "count": len(records),
    }


@router.get("/api/analysis/history/{record_id}")
async def get_analysis_record(record_id: int):
    """获取单条分析记录详情"""
    with db.get_session() as session:
        from ..models import AnalysisRecord

        record = (
            session.query(AnalysisRecord).filter(AnalysisRecord.id == record_id).first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {
            "id": record.id,
            "analysis_type": record.analysis_type,
            "model_provider": record.model_provider,
            "model_name": record.model_name,
            "price_range_start": record.price_range_start.isoformat()
            if record.price_range_start
            else None,
            "price_range_end": record.price_range_end.isoformat()
            if record.price_range_end
            else None,
            "input_summary": record.input_summary,
            "result": record.get_result(),
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
