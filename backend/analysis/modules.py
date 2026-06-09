from __future__ import annotations

from backend.analysis.base import AnalysisContext, SignalMap
from backend.analysis.dde import build_dde_signal, estimate_dde_flow
from backend.analysis.macd import build_main_chart_macd_signal, build_seconds_macd_signal
from backend.analysis.wyckoff import build_wyckoff_signal


class MainChartMacdModule:
    name = "mainChartMacd"

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, object]:
        return build_main_chart_macd_signal(context.candles, context.range_name, context.interval)


class SecondsMacdModule:
    name = "secondsMacd"

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, object]:
        return build_seconds_macd_signal(context.realtime_ticks, context.quote)


class DdeModule:
    name = "ddeFlow"

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, object]:
        flow = context.fund_flow or estimate_dde_flow(context.quote, context.candles, context.order_book)
        return build_dde_signal(flow)


class WyckoffModule:
    name = "wyckoff"

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, object]:
        dde_signal = signals.get("ddeFlow") or {}
        main_chart_macd = signals.get("mainChartMacd") or {}
        return build_wyckoff_signal(context.candles, dde_signal.get("metrics") or context.fund_flow, main_chart_macd)


DEFAULT_ANALYSIS_MODULES = (
    MainChartMacdModule(),
    SecondsMacdModule(),
    DdeModule(),
    WyckoffModule(),
)
