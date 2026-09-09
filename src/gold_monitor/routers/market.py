"""Market service HTTP adapters."""

from typing import Literal
from fastapi import APIRouter, Depends, Query
from ..dependencies import get_runtime
from ..schemas import ExchangeRateResponse, BankPricesResponse

router = APIRouter()


@router.get("/api/exchange-rate", response_model=ExchangeRateResponse)
async def get_exchange_rate(runtime=Depends(get_runtime)):
    return await runtime.market.exchange_rate()


@router.get("/api/bank-prices", response_model=BankPricesResponse)
async def get_bank_prices(runtime=Depends(get_runtime)):
    return await runtime.market.bank_prices()


@router.get("/api/convert")
async def convert_gold_price(
    price: float = Query(..., ge=0),
    from_unit: Literal["oz", "g", "kg"] = "oz",
    to_unit: Literal["oz", "g", "kg"] = "g",
    from_currency: Literal["USD", "CNY"] = "USD",
    to_currency: Literal["USD", "CNY"] = "CNY",
    runtime=Depends(get_runtime),
):
    return await runtime.market.convert(
        price, from_unit, to_unit, from_currency, to_currency
    )
