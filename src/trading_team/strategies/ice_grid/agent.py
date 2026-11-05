"""ICE/Grid agent report for debate floor consensus."""
from __future__ import annotations

from typing import Any

from trading_team.strategies.ice_grid.bridge import IceGridBridge, GRID_TYPE_NAMES


def _bars_from_snapshot(snapshot: Any) -> list[dict]:
    bars = []
    candles = None
    if hasattr(snapshot, "candles") and isinstance(snapshot.candles, dict):
        for tf in ("M5", "M15", "H1", "M1"):
            if snapshot.candles.get(tf):
                candles = snapshot.candles[tf]
                break
    elif isinstance(snapshot, dict):
        candles = snapshot.get("candles") or snapshot.get("bars")
        if isinstance(candles, dict):
            candles = candles.get("M5") or candles.get("M15") or next(iter(candles.values()), [])
    if not candles:
        return bars
    for b in candles:
        if isinstance(b, dict):
            bars.append({
                "open": float(b.get("open", b.get("o", 0))),
                "high": float(b.get("high", b.get("h", 0))),
                "low": float(b.get("low", b.get("l", 0))),
                "close": float(b.get("close", b.get("c", 0))),
                "volume": float(b.get("volume", b.get("v", 1))),
            })
        else:
            bars.append({
                "open": float(getattr(b, "open", 0)),
                "high": float(getattr(b, "high", 0)),
                "low": float(getattr(b, "low", 0)),
                "close": float(getattr(b, "close", 0)),
                "volume": float(getattr(b, "volume", 1)),
            })
    return bars


def ice_grid_agent_report(snapshot: Any, equity: float = 10_000.0, grid_profile: str = "SmartRegime") -> dict[str, Any]:
    """
    Produce a debate-floor compatible report.
    Risk gates must still override; this never bypasses kill-switch.
    """
    bridge = IceGridBridge(
        ice_config={"equity": equity, "atr_risk_frac": 0.01},
        default_grid=grid_profile if grid_profile in GRID_TYPE_NAMES else "ATRAdaptive",
    )
    bars = _bars_from_snapshot(snapshot)
    if len(bars) < 30:
        return {
            "agent": "ICE_GRID",
            "bias": "FLAT",
            "confidence": 0.0,
            "quality": 0.3,
            "reasons": ["Insufficient bars for ICE confluence"],
            "ice_grid": None,
        }
    result = bridge.evaluate_bars(bars)
    ice = result["ice"]
    side = ice["signal"]
    bias = "LONG" if side > 0 else ("SHORT" if side < 0 else "FLAT")
    reasons = [f"{ly['name']}={ly['side']:+d}:{ly['score']:.2f}" for ly in ice.get("layers") or []]
    reasons.append(f"grid={result['grid'].get('type')} levels={len(result['grid'].get('levels') or [])}")
    reasons.append(f"backend={result.get('backend')}")
    return {
        "agent": "ICE_GRID",
        "bias": bias,
        "confidence": float(ice.get("confidence") or 0.0),
        "quality": 0.75 if ice.get("pass") else 0.45,
        "reasons": reasons,
        "ice_grid": result,
        "execution_hint": result.get("execution_hint"),
    }
