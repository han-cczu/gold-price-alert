"""行情换算相关路由：/api/exchange-rate、/api/bank-prices、/api/convert"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query

from ..schemas import (
    ExchangeRateResponse, BankPriceResponse, BankPricesResponse
)
from ..state import _exchange_rate_cache

router = APIRouter()


@router.get("/api/exchange-rate", response_model=ExchangeRateResponse)
async def get_exchange_rate():
    """获取 USD/CNY 汇率"""
    import httpx
    from datetime import timedelta

    # 检查缓存是否有效（30分钟）
    if (_exchange_rate_cache["updated_at"] and
            datetime.now(timezone.utc).replace(tzinfo=None) - _exchange_rate_cache["updated_at"] < timedelta(minutes=30)):
        return ExchangeRateResponse(
            usd_cny=_exchange_rate_cache["usd_cny"],
            updated_at=_exchange_rate_cache["updated_at"]
        )

    # 尝试从免费 API 获取汇率
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 使用央行汇率接口或其他免费接口
            resp = await client.get(
                "https://api.exchangerate-api.com/v4/latest/USD"
            )
            if resp.status_code == 200:
                data = resp.json()
                rate = data.get("rates", {}).get("CNY", 7.2)
                _exchange_rate_cache["usd_cny"] = rate
                _exchange_rate_cache["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception:
        # 获取失败使用缓存或默认值
        if _exchange_rate_cache["updated_at"] is None:
            _exchange_rate_cache["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)

    return ExchangeRateResponse(
        usd_cny=_exchange_rate_cache["usd_cny"],
        updated_at=_exchange_rate_cache["updated_at"]
    )


@router.get("/api/bank-prices", response_model=BankPricesResponse)
async def get_bank_prices():
    """获取各银行金价"""
    from ..data_sources.bank import get_bank_source

    bank_source = get_bank_source()
    bank_prices = await bank_source.fetch_all_bank_prices()
    base_price = await bank_source.fetch_base_price_cny()

    # 计算伦敦金人民币价格（作为基准）
    london_gold_cny = base_price

    return BankPricesResponse(
        data=[
            BankPriceResponse(
                bank_name=p.bank_name,
                bank_code=p.bank_code,
                buy_price=p.buy_price,
                sell_price=p.sell_price,
                timestamp=p.timestamp,
                product_name=p.product_name
            ) for p in bank_prices
        ],
        base_price_cny=base_price,
        london_gold_cny=london_gold_cny,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None)
    )


@router.get("/api/convert")
async def convert_gold_price(
    price: float = Query(..., description="价格"),
    from_unit: Literal["oz", "g", "kg"] = Query(default="oz", description="原单位: oz, g, kg"),
    to_unit: Literal["oz", "g", "kg"] = Query(default="g", description="目标单位: oz, g, kg"),
    from_currency: Literal["USD", "CNY"] = Query(default="USD", description="原币种: USD, CNY"),
    to_currency: Literal["USD", "CNY"] = Query(default="CNY", description="目标币种: USD, CNY")
):
    """金价单位和币种换算"""
    # 单位换算系数（相对于盎司）
    unit_factors = {
        "oz": 1.0,
        "g": 31.1035,
        "kg": 0.0311035
    }

    # 获取汇率
    usd_cny = _exchange_rate_cache.get("usd_cny") or 7.2
    currency_rates = {
        ("USD", "CNY"): usd_cny,
        ("CNY", "USD"): 1 / usd_cny,
        ("USD", "USD"): 1.0,
        ("CNY", "CNY"): 1.0
    }

    # 先转换单位
    from_factor = unit_factors.get(from_unit, 1.0)
    to_factor = unit_factors.get(to_unit, 1.0)
    converted_price = price * from_factor / to_factor

    # 再转换币种
    rate = currency_rates.get((from_currency, to_currency), 1.0)
    converted_price *= rate

    return {
        "original": {
            "price": price,
            "unit": from_unit,
            "currency": from_currency
        },
        "converted": {
            "price": round(converted_price, 2),
            "unit": to_unit,
            "currency": to_currency
        },
        "exchange_rate": usd_cny
    }
