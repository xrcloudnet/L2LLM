from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.analysis.base import AnalysisContext, SignalMap
from backend.analysis.utils import clamp, finite, numeric_series


def label_by_score(score: float, high: str = "高", mid: str = "中", low: str = "低") -> str:
    if score >= 70:
        return high
    if score >= 45:
        return mid
    return low


def build_composite_signals(context: AnalysisContext, module_signals: SignalMap) -> SignalMap:
    df = pd.DataFrame(context.candles)
    main_chart_macd = module_signals.get("mainChartMacd") or {}
    seconds_macd = module_signals.get("secondsMacd") or {}
    dde_signal = module_signals.get("ddeFlow") or {}
    wyckoff = module_signals.get("wyckoff") or {}
    if df.empty:
        return {
            "mainAccumulation": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "hotMoneyIgnition": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "mainChartMacd": main_chart_macd,
            "secondsMacd": seconds_macd,
            "wyckoff": wyckoff,
            "ddeFlow": dde_signal,
            "bullTrap": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "limitUpProbability": {"score": 0, "label": "低", "reasons": ["K线数据不足"]},
            "riskLevel": {"score": 60, "label": "中", "reasons": ["K线数据不足，风险默认偏中"]},
        }

    quote = context.quote
    order_book = context.order_book
    metrics = context.metrics
    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    amount = numeric_series(df, "amount")

    last = finite(quote.get("price")) or finite(close.iloc[-1])
    previous = finite(quote.get("previousClose"))
    open_price = finite(quote.get("open")) or finite(open_.iloc[0])
    day_high = finite(quote.get("dayHigh")) or finite(high.max())
    day_low = finite(quote.get("dayLow")) or finite(low.min())
    change_pct = finite(quote.get("changePercent"))
    if change_pct is None and last is not None and previous:
        change_pct = (last - previous) / previous * 100

    day_amplitude = (day_high - day_low) / previous * 100 if day_high and day_low and previous else 0
    close_position = (last - day_low) / max(day_high - day_low, 0.01) if last and day_high and day_low else 0.5
    recent_return_5 = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(close) > 6 and close.iloc[-6] else 0
    volume_ratio = finite(metrics.get("volumeRatio")) or 1
    rsi14 = finite(metrics.get("rsi14")) or 50
    order_imbalance = finite(order_book.get("imbalance")) or 0
    turnover_value = finite(amount.tail(20).sum()) or finite(quote.get("amount")) or 0
    avg_amount = finite(amount.tail(20).mean()) or 0
    amount_surge = amount.iloc[-1] / max(amount.tail(30).mean(), 1) if len(amount) >= 30 and amount.tail(30).mean() else 1
    upper_shadow = (day_high - last) / max(day_high - day_low, 0.01) if day_high and day_low and last else 0
    limit_pct = 20 if previous and previous < 1 else 10
    distance_to_limit = ((previous * (1 + limit_pct / 100)) - last) / previous * 100 if previous and last else 99

    accumulation_score = 35
    accumulation_reasons = []
    if volume_ratio >= 1.25:
        accumulation_score += 18
        accumulation_reasons.append(f"量能放大 {volume_ratio:.2f}x")
    if abs(change_pct or 0) <= 2.5 and day_amplitude <= 5.5:
        accumulation_score += 18
        accumulation_reasons.append("放量但价格波动受控")
    if close_position >= 0.45 and order_imbalance >= -0.15:
        accumulation_score += 14
        accumulation_reasons.append("收盘位置不弱且盘口未明显失衡")
    if avg_amount > 0:
        accumulation_score += 8
        accumulation_reasons.append("成交额连续性可用")
    if upper_shadow > 0.45:
        accumulation_score -= 18
        accumulation_reasons.append("上影线偏长，吸筹信号打折")

    ignition_score = 20
    ignition_reasons = []
    if change_pct is not None and change_pct >= 3:
        ignition_score += 25
        ignition_reasons.append(f"涨幅达到 {change_pct:.2f}%")
    if recent_return_5 >= 1.5:
        ignition_score += 18
        ignition_reasons.append(f"近5根快速拉升 {recent_return_5:.2f}%")
    if amount_surge >= 1.8 or volume_ratio >= 1.8:
        ignition_score += 20
        ignition_reasons.append("分时成交明显脉冲放大")
    if close_position >= 0.72:
        ignition_score += 12
        ignition_reasons.append("价格靠近日内高位")
    if order_imbalance > 0.2:
        ignition_score += 10
        ignition_reasons.append("买盘量差占优")

    dde_metrics = dde_signal.get("metrics") or {}
    dde_score = finite(dde_signal.get("score")) or 50
    dde_main_ratio = finite(dde_metrics.get("mainNetInflowRatio")) or 0
    if dde_score >= 65:
        accumulation_score += 10
        accumulation_reasons.append(f"DDE资金流{dde_signal.get('label', '')}")
    if dde_score >= 72 and dde_main_ratio > 0:
        ignition_score += 8
        ignition_reasons.append("DDE大单资金配合上攻")
    if dde_score <= 35 and dde_main_ratio < 0:
        accumulation_score -= 10
        accumulation_reasons.append("DDE显示主力净流出")

    bull_trap_score = 18
    bull_trap_reasons = []
    if upper_shadow >= 0.45 and change_pct and change_pct > 0:
        bull_trap_score += 26
        bull_trap_reasons.append("冲高回落，上影线偏长")
    if volume_ratio >= 1.6 and close_position <= 0.45:
        bull_trap_score += 24
        bull_trap_reasons.append("放量但收盘位置偏低")
    if rsi14 >= 72:
        bull_trap_score += 14
        bull_trap_reasons.append(f"RSI {rsi14:.1f} 偏热")
    if order_imbalance < -0.15:
        bull_trap_score += 12
        bull_trap_reasons.append("卖盘量差占优")
    if dde_score <= 35:
        bull_trap_score += 10
        bull_trap_reasons.append("DDE资金流偏流出")
    if change_pct is not None and change_pct < -1:
        bull_trap_score -= 8

    limit_score = 8
    limit_reasons = []
    if change_pct is not None:
        limit_score += clamp(change_pct * 6, -15, 45)
        limit_reasons.append(f"当前涨幅 {change_pct:.2f}%")
    if distance_to_limit <= 3:
        limit_score += 24
        limit_reasons.append(f"距涨停约 {distance_to_limit:.2f}%")
    if ignition_score >= 65:
        limit_score += 16
        limit_reasons.append("点火特征较强")
    if volume_ratio >= 1.8:
        limit_score += 12
        limit_reasons.append("量能支持上攻")
    if dde_score >= 70:
        limit_score += 10
        limit_reasons.append("DDE资金流入强化封板动能")
    if bull_trap_score >= 65:
        limit_score -= 22
        limit_reasons.append("诱多风险压低封板概率")
    if close_position < 0.6:
        limit_score -= 12

    risk_score = 30
    risk_reasons = []
    if bull_trap_score >= 60:
        risk_score += 25
        risk_reasons.append("诱多信号偏强")
    if day_amplitude >= 6:
        risk_score += 15
        risk_reasons.append(f"日内振幅 {day_amplitude:.2f}% 偏大")
    if rsi14 >= 75:
        risk_score += 14
        risk_reasons.append(f"RSI {rsi14:.1f} 过热")
    if order_imbalance < -0.25:
        risk_score += 12
        risk_reasons.append("卖盘压力较大")
    if dde_score <= 32:
        risk_score += 14
        risk_reasons.append("DDE显示资金流出压力")
    if accumulation_score >= 70 and bull_trap_score < 50:
        risk_score -= 10
        risk_reasons.append("吸筹特征缓和短线风险")
    if dde_score >= 68 and bull_trap_score < 55:
        risk_score -= 8
        risk_reasons.append("DDE资金承接改善风险")
    if turnover_value <= 0:
        risk_score += 8
        risk_reasons.append("成交额数据不足")

    risk_score = clamp(risk_score)
    risk_label = "高" if risk_score >= 70 else "中" if risk_score >= 45 else "低"

    return {
        "mainAccumulation": {
            "score": round(clamp(accumulation_score)),
            "label": label_by_score(accumulation_score, "较强", "观察", "较弱"),
            "reasons": accumulation_reasons[:4] or ["暂未出现明显吸筹特征"],
        },
        "hotMoneyIgnition": {
            "score": round(clamp(ignition_score)),
            "label": label_by_score(ignition_score, "明显", "观察", "不明显"),
            "reasons": ignition_reasons[:4] or ["暂未出现明显点火脉冲"],
        },
        "mainChartMacd": main_chart_macd,
        "secondsMacd": seconds_macd,
        "wyckoff": wyckoff,
        "ddeFlow": dde_signal,
        "bullTrap": {
            "score": round(clamp(bull_trap_score)),
            "label": label_by_score(bull_trap_score, "高", "观察", "低"),
            "reasons": bull_trap_reasons[:4] or ["诱多特征暂不明显"],
        },
        "limitUpProbability": {
            "score": round(clamp(limit_score)),
            "label": label_by_score(limit_score, "较高", "中等", "较低"),
            "reasons": limit_reasons[:4] or ["距离涨停较远，封板动能不足"],
        },
        "riskLevel": {
            "score": round(risk_score),
            "label": risk_label,
            "reasons": risk_reasons[:4] or ["风险处于常规区间"],
        },
        "raw": {
            "changePercent": change_pct,
            "dayAmplitude": day_amplitude,
            "closePosition": close_position,
            "recentReturn5": recent_return_5,
            "orderImbalance": order_imbalance,
        },
    }


def detect_kline_patterns(candles: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    if len(df) < 5:
        return {
            "trend": {"label": "不足", "score": 0, "reasons": ["K线数量不足"]},
            "patterns": [],
            "supportResistance": {"support": metrics.get("support"), "resistance": metrics.get("resistance"), "position": "未知"},
            "summary": "K线数据不足，无法识别形态。",
        }

    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = numeric_series(df, "volume")

    last_open = finite(open_.iloc[-1]) or 0
    last_close = finite(close.iloc[-1]) or 0
    last_high = finite(high.iloc[-1]) or 0
    last_low = finite(low.iloc[-1]) or 0
    prev_open = finite(open_.iloc[-2]) or 0
    prev_close = finite(close.iloc[-2]) or 0
    body = abs(last_close - last_open)
    candle_range = max(last_high - last_low, 0.01)
    upper_shadow = (last_high - max(last_open, last_close)) / candle_range
    lower_shadow = (min(last_open, last_close) - last_low) / candle_range
    body_ratio = body / candle_range
    volume_ratio = (volume.iloc[-1] / max(volume.tail(30).mean(), 1)) if len(volume) >= 30 else 1
    ma5 = close.tail(5).mean()
    ma10 = close.tail(10).mean() if len(close) >= 10 else ma5
    ma20 = close.tail(20).mean() if len(close) >= 20 else ma10
    slope_5 = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(close) > 6 and close.iloc[-6] else 0
    slope_20 = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100 if len(close) > 21 and close.iloc[-21] else slope_5
    resistance = finite(high.tail(30).max())
    support = finite(low.tail(30).min())
    position = "中位"
    if support is not None and resistance is not None and last_close:
        pos_value = (last_close - support) / max(resistance - support, 0.01)
        if pos_value >= 0.75:
            position = "接近压力"
        elif pos_value <= 0.25:
            position = "接近支撑"

    trend_score = 50
    trend_reasons = []
    if ma5 > ma10 > ma20:
        trend_score += 22
        trend_reasons.append("短中期均线多头排列")
    elif ma5 < ma10 < ma20:
        trend_score -= 22
        trend_reasons.append("短中期均线空头排列")
    if slope_5 > 1:
        trend_score += 12
        trend_reasons.append(f"近5根上涨 {slope_5:.2f}%")
    elif slope_5 < -1:
        trend_score -= 12
        trend_reasons.append(f"近5根下跌 {abs(slope_5):.2f}%")
    if slope_20 > 3:
        trend_score += 10
        trend_reasons.append("20周期趋势向上")
    elif slope_20 < -3:
        trend_score -= 10
        trend_reasons.append("20周期趋势向下")

    trend_score = clamp(trend_score)
    trend_label = "上升趋势" if trend_score >= 65 else "下降趋势" if trend_score <= 35 else "震荡趋势"
    patterns = []

    def add_pattern(name: str, direction: str, strength: float, reason: str) -> None:
        patterns.append({"name": name, "direction": direction, "strength": round(clamp(strength)), "reason": reason})

    if body_ratio <= 0.18:
        add_pattern("十字星", "中性", 45 + min(volume_ratio * 8, 20), "实体很小，短线多空分歧加大")
    if lower_shadow >= 0.55 and body_ratio <= 0.35:
        add_pattern("锤头线", "看多", 58 + min(volume_ratio * 10, 25), "下影线较长，低位承接增强")
    if upper_shadow >= 0.55 and body_ratio <= 0.35:
        add_pattern("射击之星", "看空", 58 + min(volume_ratio * 10, 25), "上影线较长，冲高抛压明显")
    if last_close > last_open and prev_close < prev_open and last_close >= prev_open and last_open <= prev_close:
        add_pattern("阳包阴", "看多", 72, "最新阳线反包前一根阴线")
    if last_close < last_open and prev_close > prev_open and last_open >= prev_close and last_close <= prev_open:
        add_pattern("阴包阳", "看空", 72, "最新阴线吞没前一根阳线")
    if len(close) >= 3:
        c1, c2, c3 = close.iloc[-3], close.iloc[-2], close.iloc[-1]
        o1, o2, o3 = open_.iloc[-3], open_.iloc[-2], open_.iloc[-1]
        if c1 < o1 and abs(c2 - o2) / max(high.iloc[-2] - low.iloc[-2], 0.01) < 0.3 and c3 > o3 and c3 > (o1 + c1) / 2:
            add_pattern("早晨之星", "看多", 78, "三根K线出现弱转强结构")
        if c1 > o1 and abs(c2 - o2) / max(high.iloc[-2] - low.iloc[-2], 0.01) < 0.3 and c3 < o3 and c3 < (o1 + c1) / 2:
            add_pattern("黄昏之星", "看空", 78, "三根K线出现强转弱结构")
    if resistance and last_close > resistance * 0.995 and volume_ratio >= 1.4:
        add_pattern("放量突破", "看多", 76, "价格接近或突破近期压力且量能放大")
    if support and last_close < support * 1.005 and volume_ratio >= 1.4:
        add_pattern("放量破位", "看空", 76, "价格接近或跌破近期支撑且量能放大")

    patterns = sorted(patterns, key=lambda item: item["strength"], reverse=True)[:5]
    if not patterns:
        add_pattern("无明显形态", "中性", 30, "最近K线未形成高置信度经典形态")

    summary = f"{trend_label}，当前位置{position}。"
    if patterns:
        top = patterns[0]
        summary += f" 主要形态：{top['name']}（{top['direction']}，强度{top['strength']}）。"

    return {
        "trend": {"label": trend_label, "score": round(trend_score), "reasons": trend_reasons[:4] or ["均线和斜率信号不明显"]},
        "patterns": patterns,
        "supportResistance": {"support": support, "resistance": resistance, "position": position},
        "summary": summary,
    }


def compose_analysis(context: AnalysisContext, module_signals: SignalMap) -> dict[str, Any]:
    metrics = context.metrics
    signals = build_composite_signals(context, module_signals)
    kline = detect_kline_patterns(context.candles, metrics)
    last = context.quote.get("price") or (context.candles[-1]["close"] if context.candles else None)
    score = 0.0
    reasons = []
    risks = []

    if last and metrics["ma20"]:
        above = (last - metrics["ma20"]) / metrics["ma20"]
        score += 1 if above > 0 else -1
        reasons.append(f"价格{'站上' if above > 0 else '跌破'}20周期均线 {abs(above * 100):.2f}%")
    if metrics["ma20"] and metrics["ma60"]:
        score += 1 if metrics["ma20"] > metrics["ma60"] else -1
        reasons.append(f"20周期均线{'高于' if metrics['ma20'] > metrics['ma60'] else '低于'}60周期均线")
    if metrics["rsi14"] is not None:
        if metrics["rsi14"] > 70:
            score -= 0.5
            risks.append("RSI处于偏热区间，追高回撤风险上升")
        elif metrics["rsi14"] < 30:
            score += 0.5
            risks.append("RSI处于偏冷区间，反弹和弱势延续都需要观察")
        else:
            reasons.append(f"RSI {metrics['rsi14']:.1f}，动能未进入极端区")
    if metrics["volumeRatio"] and metrics["volumeRatio"] > 1.4:
        score += 0.8 if (context.quote.get("changePercent") or 0) >= 0 else -0.8
        reasons.append(f"近5根成交量约为基准的 {metrics['volumeRatio']:.2f} 倍")
    if context.order_book.get("imbalance") is not None:
        score += context.order_book["imbalance"] * 1.5
        reasons.append(f"盘口买卖量差 {context.order_book['imbalance'] * 100:.1f}%")

    dde_flow = signals.get("ddeFlow") or {}
    dde_score = finite(dde_flow.get("score")) or 50
    if dde_score >= 68:
        score += 0.8
        reasons.append(f"DDE资金流{dde_flow.get('label', '流入')}")
    elif dde_score <= 32:
        score -= 0.8
        risks.append(f"DDE资金流{dde_flow.get('label', '流出')}")

    main_chart_macd = signals.get("mainChartMacd") or {}
    if main_chart_macd.get("action") == "BUY_BIAS":
        score += 0.9
        reasons.append("主图MACD偏多，当前主图周期动能改善")
    elif main_chart_macd.get("action") == "SELL_BIAS":
        score -= 0.9
        risks.append("主图MACD偏空，当前主图周期动能转弱")

    seconds_macd = signals.get("secondsMacd") or {}
    if seconds_macd.get("action") == "STRONG_BUY":
        score += 2
        reasons.append("秒级MACD强买：零轴上方红柱放大、突破3分钟高点并放量")
    elif seconds_macd.get("action") == "BUY":
        score += 1
        reasons.append("秒级MACD偏多，短线动能改善")
    elif seconds_macd.get("action") == "SELL":
        score -= 2
        risks.append("秒级MACD卖出/止盈，短线动能转弱")

    wyckoff = signals.get("wyckoff") or {}
    wyckoff_passed = (wyckoff.get("metrics") or {}).get("passedFilters", 0)
    if wyckoff.get("action") == "BUY":
        score += 1.2
        reasons.append(f"威科夫阶段{wyckoff.get('phase')}：{wyckoff.get('label')}，三层过滤通过{wyckoff_passed}/3")
    elif wyckoff.get("action") == "SELL":
        score -= 1.2
        risks.append(f"威科夫阶段{wyckoff.get('phase')}：{wyckoff.get('label')}，三层过滤通过{wyckoff_passed}/3")
    elif wyckoff.get("phase"):
        reasons.append(f"威科夫阶段{wyckoff.get('phase')}：{wyckoff.get('label')}")

    direction = "偏多" if score > 1.1 else "偏空" if score < -1.1 else "震荡"
    confidence = max(35, min(88, 48 + abs(score) * 13 + min(metrics.get("volumeRatio") or 1, 2) * 4))
    if direction == "偏多":
        action = "关注回踩均线后的承接，避免直接追涨。"
    elif direction == "偏空":
        action = "关注反抽压力和止损纪律，弱势下不急于抄底。"
    else:
        action = "等待放量突破或跌破区间后再提高仓位。"

    if metrics["support"] and metrics["resistance"]:
        risks.append(f"近20周期压力约 {metrics['resistance']:.2f}，支撑约 {metrics['support']:.2f}")
    risks.extend((signals.get("riskLevel") or {}).get("reasons", [])[:2])
    risks.extend(main_chart_macd.get("risks", [])[:1])
    risks.extend(seconds_macd.get("risks", [])[:2])
    risks.extend(wyckoff.get("risks", [])[:1])

    signal_summary = (
        f"主力吸筹{signals['mainAccumulation']['label']}，"
        f"游资点火{signals['hotMoneyIgnition']['label']}，"
        f"DDE资金流{signals['ddeFlow']['label']}，"
        f"诱多风险{signals['bullTrap']['label']}，"
        f"封板概率{signals['limitUpProbability']['score']}%，"
        f"风险等级{signals['riskLevel']['label']}。"
        f"K线：{kline['summary']}"
    )

    return {
        "engine": "composer",
        "symbol": context.symbol,
        "direction": direction,
        "confidence": round(confidence),
        "score": round(score, 2),
        "summary": f"{context.symbol} 当前判断为{direction}，置信度 {round(confidence)}%。{signal_summary}威科夫：阶段{wyckoff.get('phase', '--')}，{wyckoff.get('label', '--')}。{action}",
        "metrics": metrics,
        "signals": signals,
        "kline": kline,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "disclaimer": "仅用于行情研究和策略辅助，不构成投资建议。",
    }
