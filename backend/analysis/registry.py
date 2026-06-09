from __future__ import annotations

from typing import Iterable

from backend.analysis.base import AnalysisContext, AnalysisModule, SignalMap


class AnalysisRegistry:
    def __init__(self, modules: Iterable[AnalysisModule] | None = None) -> None:
        self._modules: list[AnalysisModule] = list(modules or [])

    def register(self, module: AnalysisModule) -> None:
        self._modules.append(module)

    @property
    def modules(self) -> tuple[AnalysisModule, ...]:
        return tuple(self._modules)

    def run(self, context: AnalysisContext) -> SignalMap:
        signals: SignalMap = {}
        for module in self._modules:
            signals[module.name] = module.analyze(context, signals)
        return signals
