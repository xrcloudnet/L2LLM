from __future__ import annotations

import math
from typing import Any

import pandas as pd


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def numeric_series(df: pd.DataFrame, column: str, default: float = 0) -> pd.Series:
    if column in df:
        source = df[column]
    else:
        source = pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(source, errors="coerce").fillna(default)


def compute_indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    if df.empty:
        return {"ma20": None, "ma60": None, "rsi14": None, "volumeRatio": 1, "support": None, "resistance": None}

    close = pd.to_numeric(df["close"], errors="coerce")
    volume = numeric_series(df, "volume")
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    recent_volume = volume.tail(5).mean()
    base_volume = volume.iloc[-40:-5].mean() if len(volume) > 40 else volume.mean()
    recent = df.tail(20)
    return {
        "ma20": finite(close.tail(20).mean()) if len(close) >= 20 else None,
        "ma60": finite(close.tail(60).mean()) if len(close) >= 60 else None,
        "rsi14": finite(rsi.iloc[-1]) if not rsi.empty else None,
        "volumeRatio": finite(recent_volume / base_volume) if base_volume else 1,
        "support": finite(pd.to_numeric(recent["low"], errors="coerce").min()) if not recent.empty else None,
        "resistance": finite(pd.to_numeric(recent["high"], errors="coerce").max()) if not recent.empty else None,
    }
