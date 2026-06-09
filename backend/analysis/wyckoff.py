from __future__ import annotations

from typing import Any

import pandas as pd

from backend.analysis.utils import clamp, finite, numeric_series


def insufficient_wyckoff(reason: str, risk: str) -> dict[str, Any]:
    return {
        "score": 0,
        "label": "不足",
        "action": "WAIT",
        "phase": "N/A",
        "bias": "neutral",
        "events": [],
        "filters": [],
        "reasons": [reason],
        "risks": [risk],
        "metrics": {},
    }


def build_wyckoff_signal(
    candles: list[dict[str, Any]],
    fund_flow: dict[str, Any] | None = None,
    main_chart_macd: dict[str, Any] | None = None,
) -> dict[str, Any]:
    df = pd.DataFrame(candles or [])
    if len(df) < 40:
        return insufficient_wyckoff("K线数量不足，无法稳定识别威科夫结构", "至少需要40根K线")

    close = pd.to_numeric(df.get("close"), errors="coerce")
    open_ = pd.to_numeric(df.get("open"), errors="coerce")
    high = pd.to_numeric(df.get("high"), errors="coerce")
    low = pd.to_numeric(df.get("low"), errors="coerce")
    volume = numeric_series(df, "volume").replace(0, pd.NA).ffill().fillna(1)
    valid = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}).dropna(subset=["high", "low", "close"])
    if len(valid) < 40:
        return insufficient_wyckoff("有效K线数量不足，无法稳定识别威科夫结构", "需要至少40根有效 high/low/close")

    close = valid["close"]
    high = valid["high"]
    low = valid["low"]
    volume = valid["volume"].replace(0, pd.NA).ffill().fillna(1)
    lookback = min(90, len(valid))
    recent = valid.tail(lookback).copy()
    r_high = recent["high"]
    r_low = recent["low"]
    r_volume = recent["volume"].replace(0, pd.NA).ffill().fillna(1)

    support = finite(r_low.quantile(0.12)) or finite(r_low.min()) or 0
    resistance = finite(r_high.quantile(0.88)) or finite(r_high.max()) or 0
    range_width = max(resistance - support, abs(resistance) * 0.005, 0.01)
    last_close = finite(close.iloc[-1]) or 0
    last_high = finite(high.iloc[-1]) or last_close
    last_low = finite(low.iloc[-1]) or last_close
    last_volume = finite(volume.iloc[-1]) or 1
    avg_volume = finite(volume.tail(30).mean()) or 1
    volume_ratio = last_volume / max(avg_volume, 1)
    close_position = (last_close - last_low) / max(last_high - last_low, 0.01)
    in_range = support <= last_close <= resistance
    price_position = (last_close - support) / range_width
    close_ma20 = close.rolling(20).mean()
    slope20 = (finite(close_ma20.iloc[-1]) or last_close) - (finite(close_ma20.iloc[-8]) or last_close)
    trend_up = last_close > (finite(close_ma20.iloc[-1]) or last_close) and slope20 > 0
    trend_down = last_close < (finite(close_ma20.iloc[-1]) or last_close) and slope20 < 0
    spread = (high - low).abs()
    spread_ratio = (finite(spread.iloc[-1]) or 0) / max(finite(spread.tail(30).mean()) or 1, 0.01)
    recent_low_idx = int(r_low.reset_index(drop=True).idxmin())
    recent_high_idx = int(r_high.reset_index(drop=True).idxmax())

    events: list[dict[str, Any]] = []

    def add_event(code: str, name: str, side: str, strength: float, reason: str) -> None:
        events.append({"code": code, "name": name, "side": side, "strength": round(clamp(strength)), "reason": reason})

    if recent_low_idx < lookback * 0.45:
        low_volume_ratio = finite(r_volume.iloc[recent_low_idx] / max(r_volume.mean(), 1)) or 1
        rebound = (last_close - support) / range_width
        if low_volume_ratio >= 1.4 and rebound >= 0.25:
            add_event("SC", "Selling Climax", "accumulation", 55 + low_volume_ratio * 12, "区间早期放量下杀后回到交易区间")

    if recent_high_idx < lookback * 0.45:
        high_volume_ratio = finite(r_volume.iloc[recent_high_idx] / max(r_volume.mean(), 1)) or 1
        pullback = (resistance - last_close) / range_width
        if high_volume_ratio >= 1.4 and pullback >= 0.25:
            add_event("BC", "Buying Climax", "distribution", 55 + high_volume_ratio * 12, "区间早期放量冲高后回落")

    if any(event["code"] == "SC" for event in events) and last_close >= support + range_width * 0.45:
        add_event("AR", "Automatic Rally", "accumulation", 58 + price_position * 20, "SC后出现自动反弹")
    if any(event["code"] == "BC" for event in events) and last_close <= resistance - range_width * 0.45:
        add_event("AR", "Automatic Reaction", "distribution", 58 + (1 - price_position) * 20, "BC后出现自动回落")

    retest_low = last_low <= support + range_width * 0.18 and last_close > support
    retest_high = last_high >= resistance - range_width * 0.18 and last_close < resistance
    if retest_low and volume_ratio <= 1.15 and close_position >= 0.45:
        add_event("ST", "Secondary Test", "accumulation", 62 + close_position * 18, "低量回测支撑并收回")
    if retest_high and volume_ratio <= 1.15 and close_position <= 0.55:
        add_event("ST", "Secondary Test", "distribution", 62 + (1 - close_position) * 18, "低量回测压力并回落")

    if last_low < support - range_width * 0.03 and last_close > support and close_position >= 0.62:
        add_event("Spring", "Spring", "accumulation", 72 + close_position * 18 + min(volume_ratio, 3) * 4, "跌破区间下沿后快速收回")
    if last_high > resistance + range_width * 0.03 and last_close < resistance and close_position <= 0.45:
        add_event("UT", "Upthrust", "distribution", 72 + (1 - close_position) * 18 + min(volume_ratio, 3) * 4, "突破区间上沿后回落")

    breakout_up = last_close > resistance + range_width * 0.02 and volume_ratio >= 1.2
    breakdown = last_close < support - range_width * 0.02 and volume_ratio >= 1.2
    if breakout_up and close_position >= 0.58:
        add_event("SOS", "Sign of Strength", "accumulation", 70 + min(volume_ratio, 3) * 8, "放量突破交易区间上沿")
    if breakdown and close_position <= 0.45:
        add_event("SOW", "Sign of Weakness", "distribution", 70 + min(volume_ratio, 3) * 8, "放量跌破交易区间下沿")

    had_sos = any(event["code"] == "SOS" for event in events) or close.tail(8).max() > resistance
    had_weakness = any(event["code"] in {"UT", "SOW"} for event in events) or close.tail(8).min() < support
    if had_sos and support + range_width * 0.55 <= last_close <= resistance + range_width * 0.18 and volume_ratio <= 1.25:
        add_event("LPS", "Last Point of Support", "accumulation", 68 + max(price_position, 0) * 15, "强势突破后缩量回踩")
    if had_weakness and support - range_width * 0.18 <= last_close <= resistance - range_width * 0.35 and volume_ratio <= 1.25:
        add_event("LPSY", "Last Point of Supply", "distribution", 68 + max(1 - price_position, 0) * 15, "弱势跌破或上冲失败后缩量反抽")

    acc_strength = sum(event["strength"] for event in events if event["side"] == "accumulation")
    dist_strength = sum(event["strength"] for event in events if event["side"] == "distribution")
    bias = "accumulation" if acc_strength > dist_strength * 1.15 else "distribution" if dist_strength > acc_strength * 1.15 else "neutral"

    phase = "B"
    if any(event["code"] in {"SC", "BC"} for event in events):
        phase = "A"
    if any(event["code"] == "ST" for event in events) and in_range:
        phase = "B"
    if any(event["code"] in {"Spring", "UT"} for event in events):
        phase = "C"
    if any(event["code"] in {"SOS", "SOW", "LPS", "LPSY"} for event in events):
        phase = "D"
    if (bias == "accumulation" and last_close > resistance + range_width * 0.08) or (bias == "distribution" and last_close < support - range_width * 0.08):
        phase = "E"

    structure_pass = (bias == "accumulation" and any(event["code"] in {"Spring", "SOS", "LPS"} for event in events)) or (
        bias == "distribution" and any(event["code"] in {"UT", "SOW", "LPSY"} for event in events)
    )
    effort_pass = (bias == "accumulation" and ((breakout_up and volume_ratio >= 1.2) or (retest_low and volume_ratio <= 1.3))) or (
        bias == "distribution" and ((breakdown and volume_ratio >= 1.2) or (retest_high and volume_ratio <= 1.3))
    )
    dde = fund_flow or {}
    dde_ratio = finite(dde.get("mainNetInflowRatio")) or finite(dde.get("ddx")) or 0
    macd_action = (main_chart_macd or {}).get("action")
    confirmation_pass = (bias == "accumulation" and (trend_up or dde_ratio > 0 or macd_action == "BUY_BIAS")) or (
        bias == "distribution" and (trend_down or dde_ratio < 0 or macd_action == "SELL_BIAS")
    )
    filters = [
        {"name": "结构过滤", "passed": bool(structure_pass), "reason": "事件与阶段支持方向" if structure_pass else "未出现关键威科夫事件"},
        {"name": "量价过滤", "passed": bool(effort_pass), "reason": "量价行为匹配" if effort_pass else "努力与结果不够匹配"},
        {"name": "确认过滤", "passed": bool(confirmation_pass), "reason": "趋势/资金/MACD至少一项确认" if confirmation_pass else "缺少趋势、资金或MACD确认"},
    ]
    passed_count = sum(1 for item in filters if item["passed"])

    directional_strength = acc_strength - dist_strength if bias == "accumulation" else dist_strength - acc_strength
    score = round(clamp(45 + directional_strength / max(len(events), 1) * 0.22 + passed_count * 10 + (8 if phase in {"D", "E"} else 0)))
    action = "WAIT"
    label = "震荡"
    if bias == "accumulation" and passed_count >= 2 and score >= 62:
        action = "BUY"
        label = "买入观察" if passed_count == 2 else "买入"
    elif bias == "distribution" and passed_count >= 2 and score >= 62:
        action = "SELL"
        label = "卖出/减仓" if passed_count == 2 else "卖出"
    elif bias == "accumulation":
        label = "吸筹观察"
    elif bias == "distribution":
        label = "派发观察"

    top_events = sorted(events, key=lambda item: item["strength"], reverse=True)[:5]
    reasons = [f"阶段 {phase}，{label}"] + [f"{event['code']}: {event['reason']}" for event in top_events[:3]]
    reasons.extend([item["reason"] for item in filters if item["passed"]][:2])
    risks = [item["reason"] for item in filters if not item["passed"]]
    if phase in {"A", "B"}:
        risks.append("仍处区间构造阶段，信号确认度低于D/E阶段")
    if spread_ratio >= 1.8 and volume_ratio >= 1.6 and action == "BUY":
        risks.append("大幅波动放量，需防止假突破")

    return {
        "score": score,
        "label": label,
        "action": action,
        "phase": phase,
        "bias": bias,
        "events": top_events,
        "filters": filters,
        "reasons": reasons[:6],
        "risks": risks[:5],
        "metrics": {
            "support": support,
            "resistance": resistance,
            "rangeWidth": range_width,
            "pricePosition": price_position,
            "volumeRatio": volume_ratio,
            "spreadRatio": spread_ratio,
            "accumulationStrength": acc_strength,
            "distributionStrength": dist_strength,
            "passedFilters": passed_count,
        },
    }
