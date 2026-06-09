from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from backend.analysis.utils import clamp, finite, numeric_series


def estimate_dde_flow(quote: dict[str, Any], candles: list[dict[str, Any]], order_book: dict[str, Any]) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    amount = numeric_series(df, "amount") if not df.empty else pd.Series(dtype="float64")
    volume = numeric_series(df, "volume") if not df.empty else pd.Series(dtype="float64")
    last_amount = finite(quote.get("amount")) or (finite(amount.iloc[-1]) if not amount.empty else 0) or 0
    avg_amount = finite(amount.tail(20).mean()) if not amount.empty else 0
    turnover = last_amount or avg_amount or 0
    change_pct = finite(quote.get("changePercent")) or 0
    order_imbalance = finite(order_book.get("imbalance")) or 0
    volume_ratio = 1
    if len(volume) >= 20:
        recent_volume = finite(volume.tail(5).mean()) or 0
        base_volume = finite(volume.iloc[-20:-5].mean()) or 0
        volume_ratio = recent_volume / base_volume if base_volume else 1

    flow_ratio = clamp(order_imbalance * 18 + change_pct * 0.9 + (volume_ratio - 1) * 6, -25, 25)
    main_net = turnover * flow_ratio / 100 if turnover else 0
    return {
        "source": "order-book and turnover estimate",
        "quality": "estimated",
        "estimated": True,
        "date": datetime.now(timezone.utc).isoformat(),
        "mainNetInflow": main_net,
        "mainNetInflowRatio": flow_ratio,
        "superLargeNetInflow": main_net * 0.45,
        "superLargeNetInflowRatio": flow_ratio * 0.45,
        "largeNetInflow": main_net * 0.35,
        "largeNetInflowRatio": flow_ratio * 0.35,
        "mediumNetInflow": -main_net * 0.2,
        "mediumNetInflowRatio": -flow_ratio * 0.2,
        "smallNetInflow": -main_net * 0.6,
        "smallNetInflowRatio": -flow_ratio * 0.6,
        "ddx": flow_ratio,
        "ddy": flow_ratio * max(0.6, min(1.8, volume_ratio)),
        "ddz": flow_ratio + order_imbalance * 30,
        "recentMainNetInflow": main_net,
        "recentMainNetInflowRatioAvg": flow_ratio,
    }


def calculate_large_order_net_flow(flow: dict[str, Any] | None, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate Level2-style large-order net flow from available fund-flow fields."""
    source = flow or {}
    quote = quote or {}
    super_net = finite(source.get("superLargeNetInflow"))
    large_net = finite(source.get("largeNetInflow"))
    super_ratio = finite(source.get("superLargeNetInflowRatio")) or 0
    large_ratio = finite(source.get("largeNetInflowRatio")) or 0
    main_net = finite(source.get("mainNetInflow")) or 0
    main_ratio = finite(source.get("mainNetInflowRatio")) or finite(source.get("ddx")) or 0
    amount = finite(quote.get("amount")) or 0

    estimated = bool(source.get("estimated"))
    if super_net is not None or large_net is not None:
        buy_net_amount = (super_net or 0) + (large_net or 0)
        ratio = super_ratio + large_ratio
        method = "superLarge + large"
    elif main_net:
        buy_net_amount = main_net
        ratio = main_ratio
        estimated = True
        method = "mainNet fallback"
    elif amount and main_ratio:
        buy_net_amount = amount * main_ratio / 100
        ratio = main_ratio
        estimated = True
        method = "amount * mainRatio fallback"
    else:
        buy_net_amount = 0
        ratio = 0
        estimated = True
        method = "empty"

    label = "大单净流入" if buy_net_amount > 0 else "大单净流出" if buy_net_amount < 0 else "大单均衡"
    return {
        "largeOrderNetAmount": buy_net_amount,
        "largeOrderNetRatio": ratio,
        "largeOrderNetAmountWan": buy_net_amount / 10000,
        "largeOrderDirection": label,
        "estimated": estimated,
        "method": method,
    }


def build_dde_signal(fund_flow: dict[str, Any] | None) -> dict[str, Any]:
    flow = fund_flow or {}
    ddx = finite(flow.get("ddx")) or 0
    ddy = finite(flow.get("ddy")) or 0
    ddz = finite(flow.get("ddz")) or 0
    main_ratio = finite(flow.get("mainNetInflowRatio")) or ddx
    main_net = finite(flow.get("mainNetInflow")) or 0
    super_ratio = finite(flow.get("superLargeNetInflowRatio")) or 0
    large_ratio = finite(flow.get("largeNetInflowRatio")) or 0
    large_order = calculate_large_order_net_flow(flow)

    score = 50 + main_ratio * 2.2 + ddy * 1.2 + ddz * 0.8
    score += min(12, (super_ratio + large_ratio) * 0.8) if super_ratio + large_ratio > 0 else max(-12, (super_ratio + large_ratio) * 0.8)
    score = clamp(score)

    if main_ratio >= 5 and ddz >= 5:
        label = "强流入"
    elif main_ratio >= 1.5:
        label = "流入"
    elif main_ratio <= -5 and ddz <= -5:
        label = "强流出"
    elif main_ratio <= -1.5:
        label = "流出"
    else:
        label = "均衡"

    reasons = [f"主力净流入占比 {main_ratio:.2f}%", f"DDX {ddx:.2f}，DDY {ddy:.2f}，DDZ {ddz:.2f}"]
    if main_net:
        reasons.append(f"主力净额约 {main_net / 10000:.1f} 万")
    if large_order.get("largeOrderNetAmount"):
        reasons.append(f"{large_order['largeOrderDirection']}约 {large_order['largeOrderNetAmountWan']:.1f} 万")
    if flow.get("estimated"):
        reasons.append("缺少逐笔大单数据，当前为盘口/成交额估算")
    elif flow.get("source"):
        reasons.append(f"资金流来源：{flow.get('source')}")

    return {
        "score": round(score),
        "label": label,
        "reasons": reasons[:5],
        "metrics": {
            "ddx": ddx,
            "ddy": ddy,
            "ddz": ddz,
            "mainNetInflow": main_net,
            "mainNetInflowRatio": main_ratio,
            "superLargeNetInflowRatio": super_ratio,
            "largeNetInflowRatio": large_ratio,
            **large_order,
            "estimated": bool(flow.get("estimated")) or bool(large_order.get("estimated")),
            "source": flow.get("source") or "",
        },
    }
