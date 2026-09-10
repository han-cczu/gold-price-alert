"""银行金价数据源 - 获取各大银行实时金价"""

import asyncio
import logging
import math
import httpx
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, TypedDict

from .base import BaseDataSource, PriceData

logger = logging.getLogger(__name__)


class BankDefinition(TypedDict):
    code: str
    name: str
    spread: float


@dataclass
class BankGoldPrice:
    """银行金价数据"""

    bank_name: str  # 银行名称
    bank_code: str  # 银行代码
    buy_price: float  # 买入价 (CNY/g)
    sell_price: float  # 卖出价 (CNY/g)
    timestamp: datetime  # 更新时间
    product_name: str = "Au99.99"  # 产品名称


class BankGoldDataSource(BaseDataSource):
    """银行金价数据源（模拟数据，实际可接入银行API）

    注：由于各银行没有公开的免费API，这里使用模拟数据
    实际部署时可以：
    1. 爬取银行官网数据
    2. 接入第三方金价聚合API
    3. 使用付费数据服务
    """

    # 模拟的银行基础金价（基于实时国际金价换算）
    BANKS: list[BankDefinition] = [
        {"code": "ICBC", "name": "工商银行", "spread": 0.5},
        {"code": "BOC", "name": "中国银行", "spread": 0.6},
        {"code": "CCB", "name": "建设银行", "spread": 0.55},
        {"code": "ABC", "name": "农业银行", "spread": 0.58},
        {"code": "BOCOM", "name": "交通银行", "spread": 0.52},
        {"code": "CMB", "name": "招商银行", "spread": 0.65},
    ]

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._base_price_cny: float | None = None
        self._international_price: float | None = None
        self._updated_at: datetime | None = None
        self._last_fetch_failed = True
        self._fetch_lock = asyncio.Lock()

    @property
    def updated_at(self) -> datetime | None:
        return self._updated_at

    @property
    def is_fallback(self) -> bool:
        return self._last_fetch_failed

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    @property
    def name(self) -> str:
        return "bank"

    async def fetch_price(self) -> PriceData:
        """获取基准金价（用于计算银行金价）"""
        await self.fetch_base_price_cny()
        if self._international_price is None or self._updated_at is None:
            raise ValueError("银行基准价暂不可用")
        return PriceData(
            price=self._international_price,
            currency="USD",
            timestamp=self._updated_at,
            source=self.name,
        )

    @staticmethod
    def _positive_finite(value, label: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"无效的{label}")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"无效的{label}")
        return number

    async def fetch_base_price_cny(self) -> float | None:
        """获取基础金价（CNY/克）

        只有行情与汇率均有效时才发布新快照；失败保留原值和原成功时间。
        """
        async with self._fetch_lock:
            return await self._fetch_base_price_cny()

    async def _fetch_base_price_cny(self) -> float | None:
        try:
            client = self._get_client()

            # 1. 从新浪财经获取实时国际金价 (USD/oz)
            sina_headers = {
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            sina_resp = await client.get(
                "https://hq.sinajs.cn/list=hf_GC", headers=sina_headers, timeout=5.0
            )
            sina_resp.raise_for_status()
            if sina_resp.status_code != 200:
                raise ValueError("行情接口未返回完整报价")
            match = re.search(r'hq_str_hf_GC="([^"]+)"', sina_resp.text)
            if not match:
                raise ValueError("无法解析银行基准行情")
            data = match.group(1).split(",")
            international_price = self._positive_finite(
                data[0] if data[0] else data[1], "国际金价"
            )

            # 2. 获取实时汇率
            rate_resp = await client.get(
                "https://api.exchangerate-api.com/v4/latest/USD", timeout=5.0
            )
            rate_resp.raise_for_status()
            if rate_resp.status_code != 200:
                raise ValueError("汇率接口未返回完整数据")
            usd_cny = self._positive_finite(rate_resp.json()["rates"]["CNY"], "汇率")

            # 3. 计算人民币克价
            # 1 盎司 = 31.1035 克
            base_price = self._positive_finite(
                (international_price * usd_cny) / 31.1035, "人民币基准价"
            )
            if any(round(base_price - bank["spread"], 2) <= 0 for bank in self.BANKS):
                raise ValueError("银行参考买入价必须大于零")
            self._base_price_cny = base_price
            self._international_price = international_price
            self._updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self._last_fetch_failed = False

        except Exception as e:
            self._last_fetch_failed = True
            logger.warning(
                "获取银行基准金价失败，%s: %s",
                "沿用上次成功报价"
                if self._base_price_cny is not None
                else "暂无有效报价",
                type(e).__name__,
            )

        return self._base_price_cny

    async def fetch_all_bank_prices(self) -> list[BankGoldPrice]:
        """获取所有银行金价"""
        base_price = await self.fetch_base_price_cny()
        if base_price is None or self._updated_at is None:
            return []

        prices = []
        for bank in self.BANKS:
            spread = bank["spread"]
            # 买入价略低，卖出价略高
            buy_price = round(base_price - spread, 2)
            sell_price = round(base_price + spread, 2)

            prices.append(
                BankGoldPrice(
                    bank_name=bank["name"],
                    bank_code=bank["code"],
                    buy_price=buy_price,
                    sell_price=sell_price,
                    timestamp=self._updated_at,
                    product_name="Au99.99",
                )
            )

        return prices

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# 全局银行金价数据源实例
_bank_source: Optional[BankGoldDataSource] = None


def get_bank_source() -> BankGoldDataSource:
    """获取银行金价数据源单例"""
    global _bank_source
    if _bank_source is None:
        _bank_source = BankGoldDataSource()
    return _bank_source
