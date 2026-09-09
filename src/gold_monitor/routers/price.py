"""Price HTTP contracts; acquisition and querying live in PriceService."""

from fastapi import APIRouter, Depends, HTTPException, Query
from ..dependencies import get_runtime
from ..schemas import ChartDataResponse, PriceResponse, PriceHistoryResponse

router = APIRouter()


def price_response(record):
    return PriceResponse(
        price=record.price,
        currency=record.currency,
        source=record.source,
        timestamp=record.timestamp,
    )


@router.get("/api/chart/data", response_model=ChartDataResponse)
async def get_chart_data(
    hours: int = Query(24, ge=1, le=43800), runtime=Depends(get_runtime)
):
    return await runtime.prices.chart(hours)


@router.get("/api/price/current", response_model=PriceResponse)
async def get_current_price(runtime=Depends(get_runtime)):
    return price_response(await runtime.prices.current())


@router.get("/api/price/latest", response_model=PriceResponse)
async def get_latest_price(runtime=Depends(get_runtime)):
    record = await runtime.prices.latest()
    if record is None:
        raise HTTPException(status_code=404, detail="没有价格数据")
    return price_response(record)


@router.get("/api/price/history", response_model=PriceHistoryResponse)
async def get_price_history(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(100, ge=1, le=1000),
    runtime=Depends(get_runtime),
):
    records = await runtime.prices.history(hours, limit)
    prices = [r.price for r in records]
    return PriceHistoryResponse(
        data=[price_response(r) for r in records],
        count=len(records),
        stats={
            "max": max(prices) if prices else 0,
            "min": min(prices) if prices else 0,
            "avg": sum(prices) / len(prices) if prices else 0,
            "count": len(prices),
        },
    )
