"""ICE + 21-grid strategy bridge.

Prefers native ``ice_grid_core`` (.pyd/.so) built via ``build_core.ps1``.
Falls back to pure-Python parity implementation so agents always work.
"""
from __future__ import annotations

from trading_team.strategies.ice_grid.bridge import (
    GRID_TYPE_NAMES,
    IceGridBridge,
    evaluate_ice,
    build_grid,
    native_available,
)

__all__ = [
    "GRID_TYPE_NAMES",
    "IceGridBridge",
    "evaluate_ice",
    "build_grid",
    "native_available",
]
