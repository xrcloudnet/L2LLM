from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from backend.db import list_market_candles
from backend.providers.common import finite, is_aggregated_interval, range_window_ms, resample_candles, synthesize_order_book


async def local_cached_market_payload(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    reason: str,
) -> dict[str, Any] | None:
    rows = await asyncio.to_thread(
        list_market_candles,
        market=market,
        symbol=symbol,
        interval=interval,
        limit=5000,
    )
    if not rows and is_aggregated_interval(interval):
        daily_rows = await asyncio.to_thread(
            list_market_candles,
            market=market,
            symbol=symbol,
            interval="1d",
            limit=5000,
        )
        daily_candles = [
            {
                "time": row.get("time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "amount": None,
            }
            for row in daily_rows
        ]
        rows = [
            {
                "time": candle.get("time"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume"),
                "provider": "local daily resample",
            }
            for candle in resample_candles(daily_candles, interval)
        ]
    if not rows:
        return None

    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    candles = [
        {
            "time": row.get("time"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "cached": True,
        }
        for row in rows
        if range_name == "all" or int(row.get("time") or 0) >= cutoff
    ]
    if not candles:
        candles = [
            {
                "time": row.get("time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "cached": True,
            }
            for row in rows[-min(len(rows), 500):]
        ]
    candles = [candle for candle in candles if all(candle.get(key) is not None for key in ["time", "open", "high", "low", "close"])]
    if not candles:
        return None

    last = candles[-1]
    previous = candles[-2]["close"] if len(candles) > 1 else last["open"]
    price = finite(last.get("close"))
    previous_close = finite(previous)
    change = price - previous_close if price is not None and previous_close is not None else None
    currency = {"cn": "CNY", "us": "USD", "hk": "HKD"}.get(market, "")
    exchange = {
        "cn": "China A-share cached history",
        "us": "US cached history",
        "hk": "Hong Kong cached history",
    }.get(market, "cached history")

    quote = {
        "price": price,
        "previousClose": previous_close,
        "open": finite(last.get("open")),
        "dayHigh": finite(last.get("high")),
        "dayLow": finite(last.get("low")),
        "volume": finite(last.get("volume")),
        "amount": None,
        "change": change,
        "changePercent": change / previous_close * 100 if change is not None and previous_close else None,
    }
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "currency": currency,
        "marketType": market,
        "marketState": "LOCAL_CACHE",
        "provider": f"Local SQLite cache (external fallback: {reason})",
        "delayed": True,
        "cached": True,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "quote": quote,
        "candles": candles,
        "orderBook": {
            **synthesize_order_book(price, candles),
            "note": "外部数据源不可用，当前读取本地 SQLite 历史K线；盘口为估算值。",
        },
        "fundFlow": None,
        "quoteTime": datetime.fromtimestamp(int(last["time"]) / 1000, timezone.utc).isoformat(),
    }


async def fallback_to_local_market(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    reason: str,
) -> dict[str, Any]:
    cached = await local_cached_market_payload(
        market=market,
        symbol=symbol,
        range_name=range_name,
        interval=interval,
        reason=reason,
    )
    if cached:
        return cached
    raise HTTPException(status_code=502, detail=f"{reason}; local SQLite cache is empty for {symbol} {interval}.")
