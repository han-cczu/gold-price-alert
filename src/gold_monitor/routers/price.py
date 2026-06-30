"""价格相关路由：/api/price/*、/api/chart/data"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from ..collector import create_data_source
from ..schemas import ChartDataResponse, PriceResponse, PriceHistoryResponse
from ..state import db

router = APIRouter()


@router.get("/api/chart/data", response_model=ChartDataResponse)
async def get_chart_data(
    hours: int = Query(default=24, ge=1, le=43800, description="查询小时数")
):
    """获取图表数据"""
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)

    if not records:
        # 如果没有数据，尝试获取最新价格
        try:
            source = create_data_source()
            price_data = await source.fetch_price()
            db.save_price(price_data.price, price_data.source)
            latest_record = db.get_latest_price()
            records = [latest_record] if latest_record else []
        except Exception:
            return ChartDataResponse(
                timestamps=[],
                prices=[],
                current_price=0,
                price_change=0,
                price_change_percent=0,
                high=0,
                low=0
            )

    # 根据时间范围调整时间格式
    if hours <= 24:
        time_format = "%H:%M"
    elif hours <= 168:
        time_format = "%m-%d %H:%M"
    else:
        time_format = "%Y-%m-%d"

    timestamps = [r.timestamp.strftime(time_format) for r in records]
    prices = [r.price for r in records]

    current_price = prices[-1] if prices else 0
    first_price = prices[0] if prices else 0
    price_change = current_price - first_price
    price_change_percent = (price_change / first_price * 100) if first_price > 0 else 0

    return ChartDataResponse(
        timestamps=timestamps,
        prices=prices,
        current_price=current_price,
        price_change=price_change,
        price_change_percent=price_change_percent,
        high=max(prices) if prices else 0,
        low=min(prices) if prices else 0
    )


@router.get("/api/price/current", response_model=PriceResponse)
async def get_current_price():
    """获取当前金价"""
    source = create_data_source()
    price_data = await source.fetch_price()

    # 保存到数据库
    db.save_price(price_data.price, price_data.source)

    return PriceResponse(
        price=price_data.price,
        source=price_data.source,
        timestamp=price_data.timestamp
    )


@router.get("/api/price/latest", response_model=PriceResponse)
async def get_latest_price():
    """获取最新存储的金价（不请求数据源）"""
    record = db.get_latest_price()
    if not record:
        raise HTTPException(status_code=404, detail="没有价格数据")

    return PriceResponse(
        price=record.price,
        source=record.source,
        timestamp=record.timestamp
    )


@router.get("/api/price/history", response_model=PriceHistoryResponse)
async def get_price_history(
    hours: int = Query(default=24, ge=1, le=168, description="查询小时数"),
    limit: int = Query(default=100, ge=1, le=1000, description="最大记录数")
):
    """获取历史价格"""
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)
    if limit and len(records) > limit:
        records = records[-limit:]

    prices = [r.price for r in records] if records else []
    stats = {
        "max": max(prices) if prices else 0,
        "min": min(prices) if prices else 0,
        "avg": sum(prices) / len(prices) if prices else 0,
        "count": len(prices)
    }

    return PriceHistoryResponse(
        data=[
            PriceResponse(
                price=r.price,
                source=r.source,
                timestamp=r.timestamp
            ) for r in records
        ],
        count=len(records),
        stats=stats
    )
