from __future__ import annotations

from typing import Any

from backend.analysis.base import AnalysisContext
from backend.analysis.composer import compose_analysis
from backend.analysis.modules import DEFAULT_ANALYSIS_MODULES
from backend.analysis.registry import AnalysisRegistry
from backend.analysis.utils import compute_indicators


DEFAULT_REGISTRY = AnalysisRegistry(DEFAULT_ANALYSIS_MODULES)


def build_context(
    symbol: str,
    quote: dict[str, Any],
    candles: list[dict[str, Any]],
    order_book: dict[str, Any],
    fund_flow: dict[str, Any] | None,
    realtime_ticks: list[dict[str, Any]] | None,
    range_name: str,
    interval: str,
) -> AnalysisContext:
    return AnalysisContext(
        symbol=symbol,
        quote=quote,
        candles=candles,
        order_book=order_book,
        fund_flow=fund_flow,
        realtime_ticks=realtime_ticks or [],
        range_name=range_name,
        interval=interval,
        metrics=compute_indicators(candles),
    )


def run_local_analysis(
    symbol: str,
    quote: dict[str, Any],
    candles: list[dict[str, Any]],
    order_book: dict[str, Any],
    fund_flow: dict[str, Any] | None = None,
    realtime_ticks: list[dict[str, Any]] | None = None,
    range_name: str = "",
    interval: str = "",
    registry: AnalysisRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Run local analysis through modules plus the composer."""

    context = build_context(symbol, quote, candles, order_book, fund_flow, realtime_ticks, range_name, interval)
    module_signals = registry.run(context)
    result = compose_analysis(context, module_signals)
    result["analysisArchitecture"] = {
        "engine": "modular-composer",
        "modules": [module.name for module in registry.modules],
        "composer": "backend.analysis.composer",
    }
    return result
