"""Modular local analysis package.

The package is introduced as a compatibility layer first: API output stays the
same while analysis modules become independently registerable.
"""

from backend.analysis.engine import run_local_analysis

__all__ = ["run_local_analysis"]
