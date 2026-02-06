"""GoldAPI.io 数据源"""

import httpx
from datetime import datetime, timezone
from .base import BaseDataSource, PriceData


class GoldAPIDataSource(BaseDataSource):
    """GoldAPI.io 金价数据源"""

    API_URL = "https://www.goldapi.io/api/XAU/USD"

    def __init__(self, api_key: str):
        """
        初始化 GoldAPI 数据源

        Args:
            api_key: GoldAPI.io API Key
        """
        if not api_key:
            raise ValueError("GoldAPI requires an API key")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端（延迟初始化）"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    @property
    def name(self) -> str:
        return "goldapi"

    async def fetch_price(self) -> PriceData:
        """从 GoldAPI.io 获取金价"""
        headers = {
            "x-access-token": self._api_key,
            "Content-Type": "application/json"
        }

        client = self._get_client()
        response = await client.get(self.API_URL, headers=headers)
        response.raise_for_status()

        data = response.json()
        price = data.get("price")

        if price is None:
            raise ValueError("GoldAPI 返回数据无效")

        return PriceData(
            price=round(float(price), 2),
            currency="USD",
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            source=self.name
        )

    async def close(self):
        """关闭HTTP客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def __del__(self):
        """析构函数 - 尝试清理资源"""
        if self._client and not self._client.is_closed:
            pass
