"""Quota stores. Redis Lua scripts make check-and-record atomic across every worker process —
the only way to guarantee a 24-hour rolling cap when several workers publish for one account."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

from acvm.ports import QuotaDecision
from acvm.quant.ratelimit import SlidingWindowLog, TokenBucket

# KEYS[1]=zset  ARGV: now, window, limit, token
_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
if redis.call('ZSCORE', key, ARGV[4]) then
  return {1, redis.call('ZCARD', key), 0}
end
local used = redis.call('ZCARD', key)
if used < limit then
  redis.call('ZADD', key, now, ARGV[4])
  redis.call('EXPIRE', key, math.ceil(window) + 60)
  return {1, used + 1, 0}
end
local oldest = redis.call('ZRANGE', key, used - limit, used - limit, 'WITHSCORES')
local retry = 0
if oldest[2] then retry = tonumber(oldest[2]) + window - now end
return {0, used, tostring(retry)}
"""

# KEYS[1]=hash  ARGV: now, capacity, refill_per_second
_BUCKET_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local rate = tonumber(ARGV[3])
local st = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(st[1]) or cap
local ts = tonumber(st[2]) or now
if now > ts then tokens = math.min(cap, tokens + (now - ts) * rate); ts = now end
local ok = 0
local retry = 0
if tokens >= 1 then tokens = tokens - 1; ok = 1
elseif rate > 0 then retry = (1 - tokens) / rate end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(ts))
if rate > 0 then redis.call('EXPIRE', key, math.ceil(cap / rate) + 60) end
return {ok, tostring(tokens), tostring(retry)}
"""


class MemoryQuotaStore:
    def __init__(self) -> None:
        self._windows: dict[str, SlidingWindowLog] = {}
        self._tokens: dict[str, dict[str, float]] = defaultdict(dict)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire_window(self, key: str, limit: int, window_seconds: float, now: datetime,
                             token: str) -> QuotaDecision:
        ts = now.timestamp()
        async with self._lock:
            win = self._windows.setdefault(key, SlidingWindowLog(limit, window_seconds))
            win.limit, win.window_seconds = limit, window_seconds
            win._evict(ts)
            live = {k: v for k, v in self._tokens[key].items() if v > ts - window_seconds}
            self._tokens[key] = live
            if token in live:
                return QuotaDecision(True, win.count(ts), limit)
            if win.try_acquire(ts):
                live[token] = ts
                return QuotaDecision(True, win.count(ts), limit)
            return QuotaDecision(False, win.count(ts), limit, max(0.0, win.next_available(ts) - ts))

    async def release_window(self, key: str, token: str) -> None:
        async with self._lock:
            ts = self._tokens[key].pop(token, None)
            win = self._windows.get(key)
            if ts is not None and win is not None and ts in win.events:
                win.events.remove(ts)

    async def window_usage(self, key: str, window_seconds: float, now: datetime) -> int:
        async with self._lock:
            win = self._windows.get(key)
            return 0 if win is None else sum(1 for e in win.events if e > now.timestamp() - window_seconds)

    async def take_token(self, key: str, capacity: float, refill_per_second: float,
                         now: datetime) -> QuotaDecision:
        ts = now.timestamp()
        async with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = TokenBucket(capacity, refill_per_second, updated_at=ts)
            ok = b.try_consume(ts)
            return QuotaDecision(ok, int(capacity - b.tokens), int(capacity), 0.0 if ok else b.wait_time(ts))


class RedisQuotaStore:
    PREFIX = "acvm:q:"

    def __init__(self, redis: Any) -> None:
        self.redis = redis
        self._win = redis.register_script(_WINDOW_LUA)
        self._bucket = redis.register_script(_BUCKET_LUA)

    async def acquire_window(self, key: str, limit: int, window_seconds: float, now: datetime,
                             token: str) -> QuotaDecision:
        ok, used, retry = await self._win(keys=[self.PREFIX + "w:" + key],
                                          args=[now.timestamp(), window_seconds, limit, token])
        return QuotaDecision(bool(int(ok)), int(used), limit, float(retry))

    async def release_window(self, key: str, token: str) -> None:
        await self.redis.zrem(self.PREFIX + "w:" + key, token)

    async def window_usage(self, key: str, window_seconds: float, now: datetime) -> int:
        k = self.PREFIX + "w:" + key
        return int(await self.redis.zcount(k, now.timestamp() - window_seconds, "+inf"))

    async def take_token(self, key: str, capacity: float, refill_per_second: float,
                         now: datetime) -> QuotaDecision:
        ok, tokens, retry = await self._bucket(keys=[self.PREFIX + "b:" + key],
                                               args=[now.timestamp(), capacity, refill_per_second])
        return QuotaDecision(bool(int(ok)), int(capacity - float(tokens)), int(capacity), float(retry))
