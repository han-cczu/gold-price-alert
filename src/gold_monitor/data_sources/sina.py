"""新浪财经数据源"""

import httpx
import re
from datetime import datetime, timezone
from .base import BaseDataSource, PriceData


class SinaDataSource(BaseDataSource):
    """新浪财经金价数据源"""

    # 新浪财经黄金接口
    API_URL = "https://hq.sinajs.cn/list=hf_GC"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端（延迟初始化）"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    @property
    def name(self) -> str:
        return "sina"

    async def fetch_price(self) -> PriceData:
        """从新浪财经获取金价"""
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        client = self._get_client()
        response = await client.get(self.API_URL, headers=headers)
        response.raise_for_status()

        # 解析返回数据
        # 格式: var hq_str_hf_GC="黄金,2050.50,..."
        text = response.text
        match = re.search(r'hq_str_hf_GC="([^"]+)"', text)

        if not match:
            raise ValueError("无法解析新浪财经数据")

        data = match.group(1).split(",")
        price = float(data[0]) if data[0] else float(data[1])

        return PriceData(
            price=round(price, 2),
            currency="USD",
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            source=self.name,
        )

    async def close(self):
        """关闭HTTP客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def __del__(self):
        """析构函数 - 尝试清理资源"""
        if self._client and not self._client.is_closed:
            # 无法在 __del__ 中执行 async 操作，只能标记
            pass
