"""
Redis 快取層 + 記憶體 Fallback（v2.1）
─────────────────────────────────────────────────────────────────
已移除：
    store_session / get_session_obj / delete_session（AI 聊天用，已刪）

保留：
    基本 KV：set / get / delete
    rate_limit_check    （Utility AI 限額用）
    記憶體 fallback + LRU 淘汰
"""
import time
from collections import OrderedDict
from typing import Optional

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

from config import Config


class RedisCache:

    def __init__(self):
        self._client = None
        self._connected = False
        self._memory: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def init(self):
        if not _REDIS_AVAILABLE or not Config.REDIS_URL:
            print("ℹ️  Redis 未設定，使用記憶體模式")
            return
        try:
            self._client = aioredis.from_url(
                Config.REDIS_URL, decode_responses=True, socket_connect_timeout=5,
            )
            await self._client.ping()
            self._connected = True
            print("✅ Redis 連線成功！")
        except Exception as e:
            print(f"⚠️  Redis 連線失敗，降級為記憶體模式：{e}")

    async def close(self):
        if self._client:
            await self._client.aclose()

    @property
    def available(self) -> bool:
        return self._connected

    # ── 基本 KV ───────────────────────────────────────────
    async def set(self, key: str, value: str, ttl: Optional[int] = None):
        if self._connected:
            try:
                if ttl:
                    await self._client.setex(key, ttl, value)
                else:
                    await self._client.set(key, value)
            except Exception as e:
                print(f"[Redis] set 失敗：{e}")
            return
        self._evict_if_needed(key)
        expires_at = time.time() + ttl if ttl else float('inf')
        self._memory[key] = (value, expires_at)
        self._memory.move_to_end(key)

    async def get(self, key: str) -> Optional[str]:
        if self._connected:
            try:
                return await self._client.get(key)
            except Exception as e:
                print(f"[Redis] get 失敗：{e}")
                return None
        if key not in self._memory:
            return None
        value, expires_at = self._memory[key]
        if time.time() > expires_at:
            del self._memory[key]
            return None
        self._memory.move_to_end(key)
        return value

    async def delete(self, key: str):
        if self._connected:
            try:
                await self._client.delete(key)
            except Exception:
                pass
            return
        self._memory.pop(key, None)

    # ── 計數器（給 Utility AI 限額用）─────────────────────
    async def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        """
        遞增計數器，如果是第一次設定就附加 TTL。
        Utility AI 限額用：每個 key 是「日期+使用者」，TTL 設成當日剩餘秒數。
        回傳：遞增後的值
        """
        if self._connected:
            try:
                count = await self._client.incr(key)
                if count == 1:
                    await self._client.expire(key, ttl_seconds)
                return count
            except Exception as e:
                print(f"[Redis] incr 失敗：{e}")
                return 0

        # 記憶體模式：用 string 存數字
        current = await self.get(key)
        new_val = (int(current) if current else 0) + 1
        await self.set(key, str(new_val), ttl=ttl_seconds if not current else None)
        return new_val

    async def rate_limit_check(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        """檢查是否超過速率限制。True = 還在限制內。"""
        count = await self.incr_with_ttl(key, window_seconds)
        return count <= limit

    # ── 內部：LRU 淘汰 ────────────────────────────────────
    def _evict_if_needed(self, new_key: str):
        if new_key in self._memory:
            return
        now = time.time()
        expired = [k for k, (_, exp) in self._memory.items() if exp < now]
        for k in expired:
            del self._memory[k]
        while len(self._memory) >= Config.MAX_CACHE_ENTRIES:
            self._memory.popitem(last=False)
