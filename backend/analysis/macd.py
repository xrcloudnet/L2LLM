from __future__ import annotations

from typing import Any

import pandas as pd

from backend.analysis.utils import clamp, finite, numeric_series


def build_realtime_bars(ticks: list[dict[str, Any]], interval_ms: int = 3000) -> list[dict[str, Any]]:
    rows = []
    for tick in ticks or []:
        timestamp = finite(tick.get("time"))
        price = finite(tick.get("price"))
        if timestamp is None or price is None:
            continue
        rows.append(
            {
                "time": int(timestamp),
                "price": price,
                "volume": finite(tick.get("volume")),
                "amount": finite(tick.get("amount")),
            }
        )
    rows = sorted(rows, key=lambda item: item["time"])
    if not rows:
        return []

    normalized = []
    previous_volume = None
    previous_amount = None
    for row in rows:
        volume = row.get("volume")
        amount = row.get("amount")
        delta_volume = 0
        delta_amount = 0
        if volume is not None and previous_volume is not None:
            delta_volume = max(0, volume - previous_volume)
        if amount is not None and previous_amount is not None:
            delta_amount = max(0, amount - previous_amount)
        normalized.append({**row, "deltaVolume": delta_volume, "deltaAmount": delta_amount})
        if volume is not None:
            previous_volume = volume
        if amount is not None:
            previous_amount = amount

    bars = []
    for row in normalized:
        bucket = row["time"] - row["time"] % interval_ms
        if not bars or bars[-1]["time"] != bucket:
            bars.append(
                {
                    "time": bucket,
                    "open": row["price"],
                    "high": row["price"],
                    "low": row["price"],
                    "close": row["price"],
                    "volume": row["deltaVolume"],
                    "amount": row["deltaAmount"],
                }
            )
            continue
        bar = bars[-1]
        bar["high"] = max(bar["high"], row["price"])
        bar["low"] = min(bar["low"], row["price"])
        bar["close"] = row["price"]
        bar["volume"] += row["deltaVolume"]
        bar["amount"] += row["deltaAmount"]

    for bar in bars:
        if not bar["volume"]:
            bar["volume"] = 1
        if not bar["amount"]:
            bar["amount"] = bar["close"] * bar["volume"]
    return bars


def build_seconds_macd_signal(realtime_ticks: list[dict[str, Any]], quote: dict[str, Any]) -> dict[str, Any]:
    bars = build_realtime_bars(realtime_ticks)
    df = pd.DataFrame(bars)
    if len(df) < 35:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["当日秒级/分时数据不足，无法计算秒级MACD"],
            "metrics": {"barCount": len(df), "source": "realtimeTicks"},
        }

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    volume = numeric_series(df, "volume")
    amount = numeric_series(df, "amount")
    times = pd.to_numeric(df.get("time", pd.Series(dtype="float64")), errors="coerce")

    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    # 秒级MACD必须以独立实时 tick 为准，主图 quote 只在 tick 缺失时兜底。
    last_price = finite(close.iloc[-1]) or finite(quote.get("price"))
    last_time = finite(times.iloc[-1]) if not times.empty else None
    if last_price is None:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["最新价不可用，无法生成MACD交易信号"],
            "metrics": {"barCount": len(df), "source": "realtimeTicks"},
        }

    if last_time is not None:
        recent_mask = times >= last_time - 180_000
        recent_high_series = high[recent_mask].iloc[:-1]
        volume_mask = times >= last_time - 60_000
        volume_base = volume[volume_mask].iloc[:-1]
    else:
        recent_high_series = high.iloc[-61:-1]
        volume_base = volume.iloc[-21:-1]

    if recent_high_series.empty:
        recent_high_series = high.iloc[-min(len(high), 61):-1]
    recent_3min_high = finite(recent_high_series.max()) if not recent_high_series.empty else None
    if volume_base.empty:
        volume_base = volume.iloc[-21:-1]

    last_volume = finite(volume.iloc[-1]) or 0
    avg_volume = finite(volume_base.mean()) if not volume_base.empty else None
    volume_multiplier = last_volume / avg_volume if avg_volume else 1
    intraday_amount = finite(amount.sum()) or 0
    intraday_volume = finite(volume.sum()) or 0
    vwap = intraday_amount / intraday_volume if intraday_amount and intraday_volume else None
    # Some A-share tick providers report volume in lots while amount is in CNY.
    # That makes VWAP roughly 100x price, so normalize before using it as a risk line.
    if vwap and last_price and vwap > last_price * 20:
        vwap = vwap / 100
    short_ema = finite(close.ewm(span=10, adjust=False).mean().iloc[-1])

    dif_now = finite(dif.iloc[-1]) or 0
    dea_now = finite(dea.iloc[-1]) or 0
    hist_values = [finite(value) or 0 for value in hist.tail(4)]
    hist_positive = hist_values[-1] > 0
    hist_expanding_3 = len(hist_values) >= 3 and hist_values[-3] > 0 and hist_values[-2] > hist_values[-3] and hist_values[-1] > hist_values[-2]
    hist_shrinking_3 = len(hist_values) >= 3 and hist_values[-3] > 0 and hist_values[-2] < hist_values[-3] and hist_values[-1] < hist_values[-2]
    cross_up = len(dif) >= 2 and dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
    cross_down = len(dif) >= 2 and dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]
    price_break_3min_high = recent_3min_high is not None and last_price > recent_3min_high
    volume_expand = volume_multiplier >= 1.5
    above_vwap = vwap is None or last_price >= vwap
    below_vwap = vwap is not None and last_price < vwap
    below_short_ema = short_ema is not None and last_price < short_ema

    strong_buy = dif_now > 0 and dea_now > 0 and hist_expanding_3 and price_break_3min_high and volume_expand
    buy = not strong_buy and (cross_up or (dif_now > dea_now and hist_positive)) and above_vwap and volume_multiplier >= 1.15
    sell = cross_down or hist_shrinking_3 or (below_vwap and below_short_ema)

    score = 45
    reasons = []
    risks = []
    if dif_now > 0 and dea_now > 0:
        score += 12
        reasons.append("DIF与DEA均在零轴上方")
    if hist_expanding_3:
        score += 20
        reasons.append("MACD红柱连续放大3根")
    if price_break_3min_high:
        score += 18
        reasons.append("股价突破最近3分钟高点")
    if volume_expand:
        score += 14
        reasons.append(f"成交量同步放大 {volume_multiplier:.2f}x")
    if above_vwap and vwap is not None:
        score += 6
        reasons.append("价格位于VWAP上方")
    if sell:
        score -= 28
        risks.append("MACD动能转弱或价格跌破短线均衡位")
    if below_vwap:
        risks.append("价格跌破VWAP，追击胜率下降")

    if strong_buy:
        label = "强买"
        action = "STRONG_BUY"
    elif buy:
        label = "买入"
        action = "BUY"
    elif sell:
        label = "卖出/止盈"
        action = "SELL"
    else:
        label = "观望"
        action = "WAIT"

    if not reasons:
        reasons.append("秒级MACD条件未形成共振")
    if action in {"STRONG_BUY", "BUY"} and not risks:
        risks.append("追击型信号需严格止损，红柱缩短或跌回突破位应撤退")

    return {
        "score": round(clamp(score)),
        "label": label,
        "action": action,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "metrics": {
            "dif": dif_now,
            "dea": dea_now,
            "hist": hist_values[-1],
            "recent3MinHigh": recent_3min_high,
            "priceBreak3MinHigh": price_break_3min_high,
            "volumeMultiplier": volume_multiplier,
            "histExpanding3": hist_expanding_3,
            "histShrinking3": hist_shrinking_3,
            "vwap": vwap,
            "shortEma10": short_ema,
            "barCount": len(df),
            "barIntervalSeconds": 3,
            "source": "realtimeTicks",
        },
    }


def build_main_chart_macd_signal(
    candles: list[dict[str, Any]],
    range_name: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    """Calculate MACD from the currently displayed main-chart candles."""
    df = pd.DataFrame(candles)
    if len(df) < 35 or "close" not in df:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["主图K线数据不足，无法计算主图MACD"],
            "metrics": {"barCount": len(df), "source": "mainCandles", "range": range_name, "interval": interval},
        }

    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 35:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["主图K线收盘价不足，无法计算主图MACD"],
            "metrics": {"barCount": len(close), "source": "mainCandles", "range": range_name, "interval": interval},
        }

    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    dif_now = finite(dif.iloc[-1]) or 0
    dea_now = finite(dea.iloc[-1]) or 0
    hist_now = finite(hist.iloc[-1]) or 0
    hist_prev = finite(hist.iloc[-2]) if len(hist) >= 2 else None
    hist_tail = [finite(value) or 0 for value in hist.tail(4)]
    cross_up = len(dif) >= 2 and dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
    cross_down = len(dif) >= 2 and dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]
    hist_expanding_3 = len(hist_tail) >= 3 and hist_tail[-3] > 0 and hist_tail[-2] > hist_tail[-3] and hist_tail[-1] > hist_tail[-2]
    hist_shrinking_3 = len(hist_tail) >= 3 and hist_tail[-3] > 0 and hist_tail[-2] < hist_tail[-3] and hist_tail[-1] < hist_tail[-2]
    hist_turn_positive = hist_now > 0 and (hist_prev is None or hist_now >= hist_prev)
    hist_turn_negative = hist_now < 0 and (hist_prev is None or hist_now <= hist_prev)

    score = 50
    reasons = []
    risks = []
    if dif_now > 0 and dea_now > 0:
        score += 14
        reasons.append("DIF与DEA位于零轴上方")
    elif dif_now < 0 and dea_now < 0:
        score -= 14
        risks.append("DIF与DEA位于零轴下方")
    if cross_up:
        score += 16
        reasons.append("主图MACD金叉")
    if cross_down:
        score -= 16
        risks.append("主图MACD死叉")
    if hist_expanding_3:
        score += 14
        reasons.append("主图MACD红柱连续放大")
    elif hist_shrinking_3:
        score -= 12
        risks.append("主图MACD红柱连续缩短")
    elif hist_turn_positive:
        score += 8
        reasons.append("主图MACD柱体偏多")
    elif hist_turn_negative:
        score -= 8
        risks.append("主图MACD柱体偏空")

    if score >= 68:
        label = "偏多"
        action = "BUY_BIAS"
    elif score <= 36:
        label = "偏空"
        action = "SELL_BIAS"
    else:
        label = "震荡"
        action = "WAIT"

    if not reasons:
        reasons.append("主图MACD尚未形成明确多头信号")
    if label != "偏空" and not risks:
        risks.append("主图MACD仍需结合量能与盘口确认")

    return {
        "score": round(clamp(score)),
        "label": label,
        "action": action,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "metrics": {
            "dif": dif_now,
            "dea": dea_now,
            "hist": hist_now,
            "histExpanding3": hist_expanding_3,
            "histShrinking3": hist_shrinking_3,
            "barCount": len(close),
            "source": "mainCandles",
            "range": range_name,
            "interval": interval,
        },
    }
