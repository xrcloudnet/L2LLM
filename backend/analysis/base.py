from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


SignalMap = dict[str, dict[str, Any]]


@dataclass(slots=True)
class AnalysisContext:
    symbol: str
    quote: dict[str, Any]
    candles: list[dict[str, Any]]
    order_book: dict[str, Any]
    fund_flow: dict[str, Any] | None = None
    realtime_ticks: list[dict[str, Any]] = field(default_factory=list)
    range_name: str = ""
    interval: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


class AnalysisModule(Protocol):
    name: str

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, Any]:
        """Return one signal payload.

        `signals` contains outputs from modules that ran earlier. Modules can
        depend on earlier signals without importing each other directly.
        """
