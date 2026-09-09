"""Analysis routes delegate work and caching to the application service."""

import asyncio
from dataclasses import asdict
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from ..dependencies import get_runtime
from ..llm_config import LLMConfigLoadError, LLMConfigPersistenceError
from ..schemas import AnalysisResponse, SmartAnalysisResponse, RefreshAnalysisRequest
from ..services.analysis import AnalysisDataError
from ..time_utils import as_utc, utcnow

router = APIRouter()


@router.get("/api/analysis", response_model=AnalysisResponse)
async def run_analysis(runtime=Depends(get_runtime)):
    try:
        report = await runtime.analysis.run_local(runtime.db)
    except AnalysisDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (LLMConfigLoadError, LLMConfigPersistenceError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="模型分析失败，请检查模型服务配置"
        ) from exc
    return AnalysisResponse(**asdict(report))


@router.get("/api/smart-analysis", response_model=SmartAnalysisResponse)
async def get_smart_analysis(runtime=Depends(get_runtime)):
    cache = await asyncio.to_thread(runtime.analysis.get_cache)
    if cache is not None:
        age = max(
            0,
            int(
                (as_utc(utcnow()) - as_utc(cache["generated_at"])).total_seconds() / 60
            ),
        )
        return SmartAnalysisResponse(**cache, is_cached=True, cache_age_minutes=age)
    try:
        result = await runtime.analysis.run_smart()
    except (LLMConfigLoadError, LLMConfigPersistenceError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="模型分析失败，请检查模型服务配置"
        ) from exc
    return SmartAnalysisResponse(**result, is_cached=False, cache_age_minutes=0)


@router.post("/api/smart-analysis/refresh")
async def refresh_smart_analysis(
    request: Optional[RefreshAnalysisRequest] = None, runtime=Depends(get_runtime)
):
    try:
        result = await runtime.analysis.run_smart(
            model=request.model if request else None, force=True
        )
        data = SmartAnalysisResponse(**result).model_dump(mode="json")
        return {"success": True, "message": "分析已刷新", "data": data}
    except (LLMConfigLoadError, LLMConfigPersistenceError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="分析失败，请检查模型服务配置"
        ) from exc


@router.get("/api/analysis/history")
async def get_analysis_history(
    limit: int = Query(default=50, ge=1, le=200),
    analysis_type: Optional[str] = Query(
        default=None, description="分析类型: volatility, smart"
    ),
    runtime=Depends(get_runtime),
):
    records = await runtime.analysis.history(runtime.db, limit, analysis_type)
    return {"records": records, "count": len(records)}


@router.get("/api/analysis/history/{record_id}")
async def get_analysis_record(record_id: int, runtime=Depends(get_runtime)):
    record = await runtime.analysis.history_record(runtime.db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record
