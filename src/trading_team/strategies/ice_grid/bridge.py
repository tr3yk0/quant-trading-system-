"""Unified bridge: native ice_grid_core or Python fallback."""
from __future__ import annotations

from typing import Any
import numpy as np

from trading_team.strategies.ice_grid.fallback import (
    GRID_TYPE_NAMES,
    IceConfig,
    build_grid as py_build_grid,
    evaluate_ice_ohlcv as py_evaluate,
)

_NATIVE = None
try:
    from trading_team.strategies.ice_grid import ice_grid_core as _NATIVE  # type: ignore
except Exception:
    try:
        import ice_grid_core as _NATIVE  # type: ignore
    except Exception:
        _NATIVE = None


def native_available() -> bool:
    return _NATIVE is not None


def evaluate_ice(
    open_: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any = None,
    config: dict | IceConfig | None = None,
) -> dict[str, Any]:
    o = np.asarray(open_, dtype=float)
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    v = np.asarray(volume if volume is not None else np.ones_like(c), dtype=float)
    cfg = IceConfig(**config) if isinstance(config, dict) else (config or IceConfig())

    if _NATIVE is not None:
        try:
            ncfg = _NATIVE.IceConfig()
            for k, val in cfg.__dict__.items():
                if hasattr(ncfg, k):
                    setattr(ncfg, k, val)
            eng = _NATIVE.ConfluenceEngine(ncfg)
            r = eng.evaluate_ohlcv(o, h, l, c, v)
            layers = []
            for ly in r.layers:
                layers.append({"name": ly.name, "side": int(ly.side), "score": float(ly.score)})
            return {
                "signal": int(r.signal),
                "confidence": float(r.confidence),
                "pass": bool(getattr(r, "passed", getattr(r, "pass", False))),
                "atr": float(r.atr),
                "risk_lots": float(r.risk_lots),
                "stop_distance": float(r.stop_distance),
                "tp_distance": float(r.tp_distance),
                "layers_aligned": int(r.layers_aligned),
                "layers": layers,
                "backend": "native",
            }
        except Exception as exc:
            out = py_evaluate(o, h, l, c, v, cfg)
            out["native_error"] = str(exc)
            return out
    return py_evaluate(o, h, l, c, v, cfg)


def build_grid(**kwargs) -> dict[str, Any]:
    if _NATIVE is not None and "grid_type" in kwargs:
        try:
            # Prefer python builder for dict uniformity; native still available for parity tests
            return py_build_grid(**kwargs)
        except Exception:
            return py_build_grid(**kwargs)
    return py_build_grid(**kwargs)


class IceGridBridge:
    """High-level facade used by agents / web / MT5 path."""

    def __init__(self, ice_config: dict | None = None, default_grid: str = "ATRAdaptive"):
        self.ice_config = ice_config or {}
        self.default_grid = default_grid
        self._grid_state: dict | None = None

    @property
    def backend(self) -> str:
        return "native" if native_available() else "python"

    def evaluate_bars(self, bars: list[dict] | dict[str, Any]) -> dict[str, Any]:
        if isinstance(bars, dict) and "close" in bars:
            o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
            v = bars.get("volume")
        else:
            # MultiTimeframeFeed returns core.models.Candle dataclasses, while CSV/API
            # callers often provide dictionaries. Normalize both without assuming .get.
            def field(bar, *names, default=0):
                if isinstance(bar, dict):
                    for name in names:
                        if name in bar and bar[name] is not None:
                            return bar[name]
                else:
                    for name in names:
                        value = getattr(bar, name, None)
                        if value is not None:
                            return value
                return default
            o = [field(b, "open", "o") for b in bars]
            h = [field(b, "high", "h") for b in bars]
            l = [field(b, "low", "l") for b in bars]
            c = [field(b, "close", "c") for b in bars]
            v = [field(b, "volume", "v", "tick_volume", default=1) for b in bars]
        ice = evaluate_ice(o, h, l, c, v, self.ice_config)
        center = float(np.asarray(c, dtype=float)[-1])
        atr = float(ice.get("atr") or 0.0)
        grid_name = self.default_grid
        # Regime switch: agents can weight grid profile by ATR
        if atr > 0 and self.default_grid == "SmartRegime":
            grid_name = "ATRAdaptive" if atr > center * 0.002 else "Geometric"
        grid = build_grid(
            grid_type=grid_name,
            center=center,
            step=max(atr * 0.5, center * 0.0005),
            atr=atr,
            equity=float(self.ice_config.get("equity", 10_000)),
        )
        self._grid_state = grid
        return {
            "ice": ice,
            "grid": grid,
            "execution_hint": {
                "side": ice["signal"],  # 1 buy, -1 sell, 0 flat
                "lots": ice["risk_lots"] if ice["pass"] else 0.0,
                "stop_distance": ice["stop_distance"],
                "tp_distance": ice["tp_distance"],
                "allow": bool(ice["pass"]),
                "lag_bars": 1,  # strict 1-bar lag for MT5 path
            },
            "backend": ice.get("backend"),
        }

    def list_grids(self) -> list[str]:
        return list(GRID_TYPE_NAMES)
