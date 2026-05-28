import json
import os
from typing import Any


def redis_enabled() -> bool:
    return os.getenv("USE_REDIS") == "1"


_redis_client = None
_redis_error: str | None = None


def redis_client():
    global _redis_client, _redis_error
    if not redis_enabled():
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis

        _redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            decode_responses=True,
        )
        _redis_client.ping()
        _redis_error = None
        return _redis_client
    except Exception as exc:
        _redis_client = None
        _redis_error = str(exc)
        return None


def redis_get(key: str) -> Any | None:
    client = redis_client()
    if client is None:
        return None
    try:
        value = client.get(f"l2llm:{key}")
        return json.loads(value) if value is not None else None
    except Exception:
        return None


def redis_set(key: str, value: Any, ttl: float) -> Any:
    client = redis_client()
    if client is None:
        return value
    try:
        # Redis is the high-frequency cache layer. Keep values JSON-only so
        # cached data can survive process restarts and be inspected externally.
        client.setex(f"l2llm:{key}", max(1, int(ttl)), json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        pass
    return value


def redis_status() -> dict[str, Any]:
    client = redis_client()
    return {
        "enabled": redis_enabled(),
        "available": client is not None,
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "db": int(os.getenv("REDIS_DB", "0")),
        "error": _redis_error,
    }
