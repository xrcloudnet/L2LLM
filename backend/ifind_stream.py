import os
import json
from datetime import datetime, timezone
from typing import Any

from backend.cache_store import redis_client, redis_get, redis_set


def ifind_push_enabled() -> bool:
    return os.getenv("USE_IFIND_PUSH", "1") != "0"


def ifind_push_ttl() -> int:
    return max(1, int(float(os.getenv("IFIND_PUSH_TTL", "6"))))


def ifind_push_stale_seconds() -> float:
    return max(1.0, float(os.getenv("IFIND_PUSH_STALE_SECONDS", "8")))


def ifind_push_key(symbol: str) -> str:
    return f"ifind:push:quote:{symbol.strip().upper()}"


def ifind_push_tick_key(symbol: str) -> str:
    return f"ifind:push:ticks:{symbol.strip().upper()}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def set_ifind_push_quote(symbol: str, payload: dict[str, Any], ttl: int | None = None) -> dict[str, Any]:
    pushed_at = payload.get("pushedAt") or utc_now_iso()
    value = {
        **payload,
        "symbol": symbol.strip().upper(),
        "provider": payload.get("provider") or "iFinD Push",
        "pushedAt": pushed_at,
    }
    redis_set(ifind_push_key(symbol), value, ttl or ifind_push_ttl())
    append_ifind_push_tick(symbol, value, ttl=ttl)
    return value


def append_ifind_push_tick(symbol: str, payload: dict[str, Any], ttl: int | None = None, max_items: int = 7200) -> None:
    client = redis_client()
    if client is None:
        return
    price = payload.get("price")
    if price is None:
        return
    tick = {
        "symbol": symbol.strip().upper(),
        "name": payload.get("name") or symbol.strip().upper(),
        "provider": payload.get("provider") or "iFinD Push",
        "quoteTime": payload.get("quoteTime"),
        "pushedAt": payload.get("pushedAt") or utc_now_iso(),
        "price": price,
        "change": payload.get("change"),
        "changePercent": payload.get("changePercent"),
        "volume": payload.get("volume"),
        "amount": payload.get("amount"),
    }
    key = f"l2llm:{ifind_push_tick_key(symbol)}"
    try:
        # Keep a bounded intraday-like queue for diagnostics and downstream
        # second-level indicators. The frontend still samples /api/realtime.
        client.rpush(key, json.dumps(tick, ensure_ascii=False, default=str))
        client.ltrim(key, -max_items, -1)
        client.expire(key, ttl or max(ifind_push_ttl(), 60))
    except Exception:
        return


def get_ifind_push_quote(symbol: str, max_age_seconds: float | None = None) -> dict[str, Any] | None:
    if not ifind_push_enabled():
        return None
    payload = redis_get(ifind_push_key(symbol))
    if not isinstance(payload, dict):
        return None
    pushed_at = parse_time(payload.get("pushedAt"))
    if pushed_at is None:
        return None
    age = (datetime.now(timezone.utc) - pushed_at.astimezone(timezone.utc)).total_seconds()
    max_age = max_age_seconds or ifind_push_stale_seconds()
    if age > max_age:
        return None
    return {**payload, "ageSeconds": age}


def get_ifind_push_ticks(symbol: str, limit: int = 200) -> list[dict[str, Any]]:
    client = redis_client()
    if client is None:
        return []
    limit = max(1, min(limit, 7200))
    key = f"l2llm:{ifind_push_tick_key(symbol)}"
    try:
        values = client.lrange(key, -limit, -1)
    except Exception:
        return []
    ticks = []
    for value in values:
        try:
            tick = json.loads(value)
        except Exception:
            continue
        if isinstance(tick, dict):
            ticks.append(tick)
    return ticks


def pushed_quote_to_ifind_shape(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": payload.get("name") or payload.get("symbol"),
        "quoteTime": payload.get("quoteTime"),
        "quote": {
            "price": payload.get("price"),
            "previousClose": payload.get("previousClose"),
            "open": payload.get("open"),
            "dayHigh": payload.get("dayHigh"),
            "dayLow": payload.get("dayLow"),
            "volume": payload.get("volume"),
            "amount": payload.get("amount"),
            "change": payload.get("change"),
            "changePercent": payload.get("changePercent"),
            "volumeRatio": payload.get("volumeRatio"),
            "committee": payload.get("committee"),
            "commissionDiff": payload.get("commissionDiff"),
            "tradeStatus": payload.get("tradeStatus"),
        },
    }
