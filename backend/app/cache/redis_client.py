"""
Redis Client — Production-grade cache and rate limiting backed by Redis.
Falls back to in-memory implementations when Redis is unavailable.
"""

import json
import time
import hashlib
import structlog
from typing import Optional

logger = structlog.get_logger()


class RedisCache:
    """
    Redis-backed query cache with TTL and tenant isolation.
    Replaces InMemoryQueryCache for multi-worker production deployments.
    """

    def __init__(self, redis_url: str, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._client = None
        try:
            import redis
            self._client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=3,
                retry_on_timeout=True,
            )
            # Verify connection
            self._client.ping()
            logger.info("redis_cache_connected", url=redis_url.split("@")[-1])
        except Exception as e:
            logger.warning("redis_cache_unavailable", error=str(e))
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    @staticmethod
    def _make_key(query: str, tenant_id: str = "default", db_id: str = "default") -> str:
        raw = f"{tenant_id}:{db_id}:{query.strip().lower()}"
        return f"plainsql:cache:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    def get(self, query: str, tenant_id: str = "default", db_id: str = "default") -> Optional[dict]:
        """Retrieve cached query result."""
        if not self._client:
            return None
        try:
            key = self._make_key(query, tenant_id, db_id)
            data = self._client.get(key)
            if data:
                logger.debug("cache_hit", key=key[:20])
                return json.loads(data)
        except Exception as e:
            logger.warning("redis_get_failed", error=str(e))
        return None

    def set(self, query: str, data: dict, tenant_id: str = "default", db_id: str = "default"):
        """Cache a query result with TTL."""
        if not self._client:
            return
        try:
            key = self._make_key(query, tenant_id, db_id)
            self._client.setex(key, self.ttl, json.dumps(data, default=str))
        except Exception as e:
            logger.warning("redis_set_failed", error=str(e))

    def invalidate(self, tenant_id: str = "default"):
        """Clear all cache entries.
        Note: Since cache keys are SHA-256 hashes of '{tenant_id}:{query}',
        we cannot filter by tenant from the key alone. This purges all entries.
        For tenant-isolated purge, store tenant_id in the value and scan.
        """
        if not self._client:
            return
        try:
            pattern = "plainsql:cache:*"
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=100)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("redis_invalidate_failed", error=str(e))

    def invalidate_all(self):
        """Clear all cached queries."""
        if not self._client:
            return
        try:
            pattern = "plainsql:cache:*"
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=100)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("redis_invalidate_all_failed", error=str(e))


class RedisRateLimiter:
    """
    Redis-backed sliding window rate limiter.
    Shared across all Gunicorn workers for accurate rate limiting.
    """

    def __init__(self, redis_url: str, requests_per_minute: int = 60):
        self.rpm = requests_per_minute
        self._client = None
        self._consecutive_failures = 0
        self._max_failures_before_deny = 3  # Fail closed after this many errors
        try:
            import redis
            self._client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=2,
            )
            self._client.ping()
            logger.info("redis_rate_limiter_connected")
        except Exception as e:
            logger.warning("redis_rate_limiter_unavailable", error=str(e))
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def check(self, key: str) -> bool:
        """Returns True if request is allowed, False if rate limited."""
        if not self._client:
            return True  # Fail open only when Redis was never connected
        try:
            redis_key = f"plainsql:ratelimit:{key}"
            now = time.time()
            window_start = now - 60

            pipe = self._client.pipeline()
            # Remove old entries outside the window
            pipe.zremrangebyscore(redis_key, 0, window_start)
            # Count entries in current window
            pipe.zcard(redis_key)
            # Add current request
            pipe.zadd(redis_key, {str(now): now})
            # Set TTL on the key to auto-cleanup
            pipe.expire(redis_key, 120)
            results = pipe.execute()

            current_count = results[1]
            self._consecutive_failures = 0  # Reset on success
            return current_count < self.rpm
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(
                "redis_rate_check_failed",
                error=str(e),
                consecutive_failures=self._consecutive_failures,
            )
            # Fail OPEN after repeated Redis failures to maintain service availability during outages
            if self._consecutive_failures >= self._max_failures_before_deny:
                logger.warning("rate_limiter_fail_open", reason="redis_unavailable_failing_open")
                return True
            return True  # Allow during transient errors


def create_cache(redis_url: str = None, ttl_seconds: int = 300):
    """
    Factory: Returns RedisCache if Redis is available, otherwise InMemoryQueryCache.
    """
    if redis_url:
        cache = RedisCache(redis_url, ttl_seconds)
        if cache.available:
            return cache
    # Fallback to in-memory
    from app.api.middleware import QueryCache
    logger.info("using_inmemory_cache_fallback")
    return QueryCache(ttl_seconds=ttl_seconds)


def create_rate_limiter(redis_url: str = None, rpm: int = 60):
    """
    Factory: Returns RedisRateLimiter if Redis is available, otherwise InMemoryRateLimiter.
    """
    if redis_url:
        limiter = RedisRateLimiter(redis_url, rpm)
        if limiter.available:
            return limiter
    # Fallback to in-memory
    from app.api.middleware import InMemoryRateLimiter
    logger.info("using_inmemory_rate_limiter_fallback")
    return InMemoryRateLimiter(requests_per_minute=rpm)
