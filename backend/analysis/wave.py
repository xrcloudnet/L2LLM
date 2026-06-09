from __future__ import annotations

from typing import Any

from backend.analysis.base import AnalysisContext, SignalMap


class WaveModule:
    """Placeholder interface for Elliott/Wave analysis.

    Keep this module disabled until the wave parser is implemented. The class
    documents the target output shape so adding it later does not require route
    or frontend contract changes.
    """

    name = "wave"

    def analyze(self, context: AnalysisContext, signals: SignalMap) -> dict[str, Any]:
        return {
            "score": 0,
            "label": "未启用",
            "action": "WAIT",
            "wave": {},
            "filters": [],
            "reasons": ["波浪理论模块尚未启用"],
            "risks": [],
            "metrics": {"barCount": len(context.candles)},
        }
