from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


CHINA_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def china_market_time_ms(value: Any) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ)
    else:
        parsed = parsed.tz_convert(CHINA_TZ)
    return int(parsed.timestamp() * 1000)


def china_market_day_ms(value: Any) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ)
    else:
        parsed = parsed.tz_convert(CHINA_TZ)
    day_start = parsed.normalize()
    return int(day_start.timestamp() * 1000)


def normalize_a_share_symbol(symbol: str) -> dict[str, str] | None:
    raw = symbol.strip().upper().replace(" ", "")
    prefixed = re.match(r"^(SH|SZ)(\d{6})$", raw)
    suffixed = re.match(r"^(\d{6})\.(SH|SZ|SS)$", raw)
    plain = re.match(r"^(\d{6})$", raw)

    code = None
    exchange = None
    if prefixed:
        exchange = prefixed.group(1).lower()
        code = prefixed.group(2)
    elif suffixed:
        code = suffixed.group(1)
        exchange = "sh" if suffixed.group(2) in {"SH", "SS"} else "sz"
    elif plain:
        code = plain.group(1)
        exchange = "sh" if code.startswith(("6", "9")) else "sz"

    if not code or not exchange:
        return None

    return {
        "code": code,
        "exchange": exchange,
        "display": f"{exchange.upper()}{code}",
        "secid": f"{1 if exchange == 'sh' else 0}.{code}",
        "sina": f"{exchange}{code}",
    }


def normalize_market_response_symbol(symbol: str) -> str:
    a_share = normalize_a_share_symbol(symbol)
    if a_share:
        return a_share["display"]
    return symbol.strip().upper()


def range_window_ms(range_name: str) -> int:
    normalized = (range_name or "").lower()
    now = datetime.now(timezone.utc)
    if normalized == "ytd":
        start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        return max(24 * 60 * 60 * 1000, int((now - start).total_seconds() * 1000))
    if normalized == "all":
        return 365 * 20 * 24 * 60 * 60 * 1000
    mapping = {
        "1d": 24 * 60 * 60 * 1000,
        "5d": 5 * 24 * 60 * 60 * 1000,
        "1mo": 32 * 24 * 60 * 60 * 1000,
        "3mo": 93 * 24 * 60 * 60 * 1000,
        "6mo": 186 * 24 * 60 * 60 * 1000,
        "1y": 366 * 24 * 60 * 60 * 1000,
        "3y": 3 * 366 * 24 * 60 * 60 * 1000,
        "5y": 5 * 366 * 24 * 60 * 60 * 1000,
        "10y": 10 * 366 * 24 * 60 * 60 * 1000,
    }
    return mapping.get(normalized, mapping["1d"])


def normalize_interval(interval: str) -> str:
    return {
        "1m": "1",
        "2m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1d": "101",
        "1wk": "102",
        "1w": "102",
        "1week": "102",
        "1mo": "103",
        "3mo": "103",
        "6mo": "103",
    }.get(interval, "1")


def is_aggregated_interval(interval: str) -> bool:
    return interval in {"1wk", "1w", "1week", "1mo", "3mo", "6mo"}


def resample_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if not candles or interval not in {"1wk", "1w", "1week", "1mo", "3mo", "6mo"}:
        return candles
    df = pd.DataFrame(candles)
    if df.empty or "time" not in df:
        return candles
    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.sort_values("datetime").set_index("datetime")
    rule = {"1wk": "W", "1w": "W", "1week": "W", "1mo": "ME", "3mo": "QE", "6mo": "2QE"}[interval]
    grouped = df.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum" if "amount" in df else "first",
        }
    )
    grouped = grouped.dropna(subset=["open", "high", "low", "close"])
    records = []
    for timestamp, row in grouped.iterrows():
        records.append(
            {
                "time": int(timestamp.timestamp() * 1000),
                "open": finite(row.get("open")),
                "high": finite(row.get("high")),
                "low": finite(row.get("low")),
                "close": finite(row.get("close")),
                "volume": finite(row.get("volume")) or 0,
                "amount": finite(row.get("amount")),
            }
        )
    return records


def df_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.where(pd.notnull(df), None).to_json(orient="records", force_ascii=False))


def numeric_series(df: pd.DataFrame, column: str, default: float = 0) -> pd.Series:
    if column in df:
        source = df[column]
    else:
        source = pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(source, errors="coerce").fillna(default)


def synthesize_order_book(price: float | None, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not price:
        return {"provider": "synthetic-depth", "bids": [], "asks": [], "imbalance": 0}
    df = pd.DataFrame(candles[-30:])
    avg_range = float((df["high"] - df["low"]).clip(lower=0).mean()) if not df.empty else price * 0.001
    tick = max(price * 0.0003, avg_range * 0.08, 0.01)
    momentum = 0.0
    if len(candles) > 5:
        momentum = (candles[-1]["close"] - candles[-5]["close"]) / candles[-5]["close"]
    bias = max(-0.35, min(0.35, momentum * 12))
    bids = [{"price": price - tick * i, "size": round((1200 + i * 170) * (1 + bias))} for i in range(1, 9)]
    asks = [{"price": price + tick * i, "size": round((1200 + i * 170) * (1 - bias))} for i in range(1, 9)]
    bid_total = sum(row["size"] for row in bids)
    ask_total = sum(row["size"] for row in asks)
    return {
        "provider": "synthetic-depth",
        "note": "美股免费源不含实时Level2盘口，此处为演示盘口；A股会优先使用 AKShare 五档盘口。",
        "bids": bids,
        "asks": asks,
        "imbalance": (bid_total - ask_total) / max(bid_total + ask_total, 1),
    }
