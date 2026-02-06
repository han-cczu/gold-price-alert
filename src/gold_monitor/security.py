"""安全模块 - 密钥管理与接口保护"""

import base64
import hashlib
import logging
import os
import secrets
from functools import wraps
from typing import Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


# ============ 密钥管理 ============

class SecretManager:
    """密钥管理器 - 加密存储敏感信息"""

    def __init__(self, secret_key: str = None):
        """
        初始化密钥管理器
        
        Args:
            secret_key: 主密钥，如果不提供则从环境变量获取或自动生成
        """
        self._key = secret_key or os.getenv("GOLD_SECRET_KEY")
        if not self._key:
            self._key = self._generate_key()
            logger.warning("未配置 GOLD_SECRET_KEY，使用自动生成的密钥（重启后将无法解密旧数据）")

        # 派生加密密钥
        self._derived_key = self._derive_key(self._key)

    def _generate_key(self) -> str:
        """生成随机密钥"""
        return secrets.token_urlsafe(32)

    def _derive_key(self, key: str) -> bytes:
        """从主密钥派生加密密钥"""
        return hashlib.pbkdf2_hmac(
            'sha256',
            key.encode(),
            b'gold_monitor_salt',
            100000,
            dklen=32
        )

    def encrypt(self, plaintext: str) -> str:
        """加密字符串（使用 XOR + Base64，轻量级方案）
        
        注意：这是一个轻量级加密方案，适用于配置文件存储
        如需更高安全性，可以使用 cryptography.fernet
        """
        if not plaintext:
            return ""

        try:
            # 生成随机 IV
            iv = secrets.token_bytes(16)

            # XOR 加密
            key_stream = self._derived_key * (len(plaintext) // 32 + 1)
            encrypted = bytes([
                ord(c) ^ key_stream[i] ^ iv[i % 16]
                for i, c in enumerate(plaintext)
            ])

            # 组合 IV + 密文
            result = iv + encrypted
            return base64.urlsafe_b64encode(result).decode()
        except Exception as e:
            logger.error("加密失败: %s", e)
            return ""

    def decrypt(self, ciphertext: str) -> str:
        """解密字符串"""
        if not ciphertext:
            return ""

        try:
            # 解码
            data = base64.urlsafe_b64decode(ciphertext.encode())
            if len(data) < 17:
                return ""

            # 提取 IV 和密文
            iv = data[:16]
            encrypted = data[16:]

            # XOR 解密
            key_stream = self._derived_key * (len(encrypted) // 32 + 1)
            decrypted = bytes([
                b ^ key_stream[i] ^ iv[i % 16]
                for i, b in enumerate(encrypted)
            ])

            return decrypted.decode()
        except Exception as e:
            logger.error("解密失败: %s", e)
            return ""

    def hash_password(self, password: str) -> str:
        """哈希密码"""
        salt = secrets.token_bytes(16)
        key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
        return base64.urlsafe_b64encode(salt + key).decode()

    def verify_password(self, password: str, hashed: str) -> bool:
        """验证密码"""
        try:
            data = base64.urlsafe_b64decode(hashed.encode())
            salt = data[:16]
            stored_key = data[16:]
            key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
            return secrets.compare_digest(key, stored_key)
        except Exception:
            return False


# ============ API 鉴权 ============

class APIKeyAuth:
    """API Key 鉴权"""

    def __init__(self, admin_api_key: str = None):
        """
        初始化 API Key 鉴权
        
        Args:
            admin_api_key: 管理接口 API Key，如果不提供则从环境变量获取
        """
        self._admin_key = admin_api_key or os.getenv("GOLD_ADMIN_API_KEY", "")
        if not self._admin_key:
            # 如果未配置，生成一个临时的（生产环境应该配置）
            self._admin_key = secrets.token_urlsafe(32)
            logger.warning(
                "未配置 GOLD_ADMIN_API_KEY，生成临时密钥: %s (请在生产环境中配置)",
                self._admin_key[:8] + "..."
            )

    @property
    def admin_key(self) -> str:
        """获取管理 API Key（用于显示或测试）"""
        return self._admin_key

    def verify_admin_key(self, request: Request) -> bool:
        """验证管理 API Key"""
        key = request.headers.get("X-Admin-Key") or request.query_params.get("admin_key")
        return key and secrets.compare_digest(key, self._admin_key)

    async def require_admin(self, request: Request):
        """要求管理员权限（作为依赖项使用）"""
        if not self._admin_key:
            # 如果未配置 API Key，则不需要验证（开发模式）
            return True

        if not self.verify_admin_key(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要管理员权限，请提供有效的 X-Admin-Key 头",
                headers={"WWW-Authenticate": "API-Key"}
            )
        return True


# ============ 限流中间件 ============

class RateLimiter:
    """简单的内存限流器"""

    def __init__(self, requests_per_minute: int = 60):
        self._limit = requests_per_minute
        self._requests: dict[str, list[float]] = {}
        self._window = 60.0  # 1分钟窗口

    def _get_client_id(self, request: Request) -> str:
        """获取客户端标识"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _cleanup_old_requests(self, client_id: str, now: float):
        """清理过期的请求记录"""
        if client_id in self._requests:
            cutoff = now - self._window
            self._requests[client_id] = [
                t for t in self._requests[client_id] if t > cutoff
            ]

    def is_allowed(self, request: Request) -> tuple[bool, int]:
        """检查是否允许请求
        
        Returns:
            (allowed, remaining): 是否允许, 剩余配额
        """
        import time
        now = time.time()
        client_id = self._get_client_id(request)

        self._cleanup_old_requests(client_id, now)

        if client_id not in self._requests:
            self._requests[client_id] = []

        request_count = len(self._requests[client_id])

        if request_count >= self._limit:
            return False, 0

        self._requests[client_id].append(now)
        return True, self._limit - request_count - 1

    async def check(self, request: Request):
        """检查限流（作为依赖项使用）"""
        allowed, remaining = self.is_allowed(request)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"请求过于频繁，请稍后再试（限制: {self._limit}/分钟）",
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"}
            )
        return remaining


# ============ 敏感路径保护 ============

# 需要管理员权限的路径前缀
ADMIN_PATHS = [
    "/api/llm/",
    "/api/collector/config",
    "/api/notifications/config",
    "/api/data/cleanup",
    "/api/data/backup",
]

# 需要限流的路径前缀
RATE_LIMITED_PATHS = [
    "/api/smart-analysis",
    "/api/analysis",
    "/api/llm/",
]


def is_admin_path(path: str) -> bool:
    """检查是否是管理员路径"""
    return any(path.startswith(prefix) for prefix in ADMIN_PATHS)


def is_rate_limited_path(path: str) -> bool:
    """检查是否需要限流"""
    return any(path.startswith(prefix) for prefix in RATE_LIMITED_PATHS)


# ============ 全局实例 ============

_secret_manager: Optional[SecretManager] = None
_api_key_auth: Optional[APIKeyAuth] = None
_rate_limiter: Optional[RateLimiter] = None


def get_secret_manager() -> SecretManager:
    """获取全局密钥管理器"""
    global _secret_manager
    if _secret_manager is None:
        _secret_manager = SecretManager()
    return _secret_manager


def get_api_key_auth() -> APIKeyAuth:
    """获取全局 API Key 鉴权器"""
    global _api_key_auth
    if _api_key_auth is None:
        _api_key_auth = APIKeyAuth()
    return _api_key_auth


def get_rate_limiter(requests_per_minute: int = 60) -> RateLimiter:
    """获取全局限流器"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(requests_per_minute)
    return _rate_limiter
