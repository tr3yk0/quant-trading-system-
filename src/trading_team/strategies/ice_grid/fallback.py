"""Pure-Python parity mirror of cpp_core ICE + Grid engines."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
import math
import numpy as np

GRID_TYPE_NAMES = [
    "Geometric", "Arithmetic", "Fibonacci", "ATRAdaptive", "Breakout", "AntiMartingale",
    "SmartRegime", "MartingaleSoft", "Inverse", "Expanding", "Contracting", "VolatilityScaled",
    "SessionAnchored", "TrendFollowingGrid", "MeanReversionGrid", "HybridEMA", "LiquiditySweep",
    "RangeBound", "MomentumBurst", "HedgeSymmetric", "RegimeSwitchBasket",
]


@dataclass
class IceConfig:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_ob: float = 70.0
    rsi_os: float = 30.0
    atr_period: int = 14
    atr_risk_frac: float = 0.01
    atr_stop_mult: float = 1.5
    atr_tp_mult: float = 2.5
    ob_lookback: int = 50
    fvg_min_atr: float = 0.15
    equity: float = 10_000.0
    contract_value: float = 1.0


def _ema(x: np.ndarray, period: int) -> float:
    if len(x) == 0:
        return 0.0
    a = 2.0 / (period + 1.0)
    e = float(x[0])
    for v in x[1:]:
        e = a * float(v) + (1.0 - a) * e
    return e


def _atr(h, l, c, period: int) -> float:
    if len(c) < 2:
        return 0.0
    trs = []
    for i in range(1, len(c)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return float(np.mean(window)) if window else 0.0


def _rsi(c: np.ndarray, period: int) -> float:
    if len(c) < period + 1:
        return 50.0
    d = np.diff(c[-(period + 1):])
    gain = d[d >= 0].sum()
    loss = -d[d < 0].sum()
    if loss < 1e-12:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def evaluate_ice_ohlcv(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray | None = None,
    cfg: IceConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or IceConfig()
    o = np.asarray(open_, dtype=float)
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    v = np.asarray(volume if volume is not None else np.ones_like(c), dtype=float)
    n = len(c)
    if n < 30:
        return {"signal": 0, "confidence": 0.0, "pass": False, "atr": 0.0, "risk_lots": 0.0,
                "stop_distance": 0.0, "tp_distance": 0.0, "layers_aligned": 0, "layers": [], "backend": "python"}

    atr = _atr(h, l, c, cfg.atr_period)
    layers = []

    # ICT SMC
    score = 0.0
    if n >= 3:
        gap_up = l[-1] - h[-3]
        gap_dn = l[-3] - h[-1]  # simplified
        gap_dn = h[-3] - l[-1]  # wait
        gap_dn = h[-3] - l[-1]
        # bear FVG: prior low above current high
        gap_dn = l[-3] - h[-1]
        if gap_up > cfg.fvg_min_atr * max(atr, 1e-8):
            score += 0.35
        if (l[-3] - h[-1]) > cfg.fvg_min_atr * max(atr, 1e-8):
            score -= 0.35
    recent_hi = float(np.max(h[-10:-1])) if n >= 10 else float(h[-2])
    recent_lo = float(np.min(l[-10:-1])) if n >= 10 else float(l[-2])
    if l[-1] < recent_lo and c[-1] > recent_lo:
        score += 0.4
    if h[-1] > recent_hi and c[-1] < recent_hi:
        score -= 0.4
    body = o[-2]  # use prev body
    body = c[-2] - o[-2]
    if body > 0.5 * atr:
        score += 0.2
    if body < -0.5 * atr:
        score -= 0.2
    score = max(-1.0, min(1.0, score))
    side = 1 if score > 0.15 else (-1 if score < -0.15 else 0)
    layers.append({"name": "ICT_SMC", "side": side, "score": score})

    # EMA
    ef, em, es = _ema(c, cfg.ema_fast), _ema(c, cfg.ema_mid), _ema(c, cfg.ema_slow)
    px = float(c[-1])
    if ef > em > es and px > ef:
        score = 0.9
    elif ef < em < es and px < ef:
        score = -0.9
    elif ef > em:
        score = 0.35
    elif ef < em:
        score = -0.35
    else:
        score = 0.0
    side = 1 if score > 0.2 else (-1 if score < -0.2 else 0)
    layers.append({"name": "MULTI_EMA", "side": side, "score": score})

    # MACD-RSI
    macd_series = []
    for i in range(len(c)):
        sub = c[: i + 1]
        macd_series.append(_ema(sub, cfg.macd_fast) - _ema(sub, cfg.macd_slow))
    macd_series = np.asarray(macd_series, dtype=float)
    macd = float(macd_series[-1])
    sig = _ema(macd_series, cfg.macd_signal)
    hist = macd - sig
    r = _rsi(c, cfg.rsi_period)
    score = 0.4 if hist > 0 else -0.4
    if r < cfg.rsi_os:
        score += 0.4
    if r > cfg.rsi_ob:
        score -= 0.4
    if 45 <= r <= 55:
        score *= 0.5
    score = max(-1.0, min(1.0, score))
    side = 1 if score > 0.2 else (-1 if score < -0.2 else 0)
    layers.append({"name": "MACD_RSI", "side": side, "score": score})

    # VWAP
    tp = (h + l + c) / 3.0
    vv = np.maximum(v, 1.0)
    vwap = float(np.sum(tp * vv) / np.sum(vv))
    dist = (px - vwap) / max(abs(vwap), 1e-8)
    score = max(-1.0, min(1.0, dist * 50.0))
    if abs(score) < 0.15:
        score = 0.0
    elif score > 0:
        score = 0.5
    else:
        score = -0.5
    side = 1 if score > 0.2 else (-1 if score < -0.2 else 0)
    layers.append({"name": "VWAP", "side": side, "score": score})

    layers.append({"name": "ATR_RISK", "side": 0, "score": 0.5 if atr > 0 else 0.0})

    buy_s = sell_s = 0.0
    buy_n = sell_n = 0
    for ly in layers[:4]:
        if ly["side"] > 0:
            buy_s += ly["score"]
            buy_n += 1
        elif ly["side"] < 0:
            sell_s += -ly["score"]
            sell_n += 1

    if buy_n >= 3 and buy_s >= sell_s:
        signal, conf, passed = 1, min(1.0, buy_s / 4.0), True
    elif sell_n >= 3 and sell_s >= buy_s:
        signal, conf, passed = -1, min(1.0, sell_s / 4.0), True
    else:
        signal, conf, passed = 0, abs(buy_s - sell_s) / 4.0, False

    stop = cfg.atr_stop_mult * atr
    tp = cfg.atr_tp_mult * atr
    lots = 0.0
    if stop > 1e-12:
        lots = (cfg.equity * cfg.atr_risk_frac) / (stop * max(cfg.contract_value, 1e-8))
        lots = max(0.01, min(lots, 50.0))

    return {
        "signal": signal,
        "confidence": conf,
        "pass": passed,
        "atr": atr,
        "risk_lots": lots,
        "stop_distance": stop,
        "tp_distance": tp,
        "layers_aligned": max(buy_n, sell_n),
        "layers": layers,
        "backend": "python",
    }


def _fib(i: int) -> int:
    F = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
    return F[min(max(i, 0), 10)]


def build_grid(
    grid_type: str | int = "Geometric",
    center: float = 0.0,
    step: float = 1.0,
    levels_up: int = 5,
    levels_down: int = 5,
    base_lots: float = 0.1,
    lot_multiplier: float = 1.25,
    atr: float = 0.0,
    atr_step_mult: float = 0.5,
    equity: float = 10_000.0,
    max_risk_frac: float = 0.05,
) -> dict[str, Any]:
    if isinstance(grid_type, int):
        name = GRID_TYPE_NAMES[grid_type] if 0 <= grid_type < 21 else "Geometric"
        idx = grid_type
    else:
        name = str(grid_type)
        idx = GRID_TYPE_NAMES.index(name) if name in GRID_TYPE_NAMES else 0

    if name in ("ATRAdaptive", "VolatilityScaled"):
        step_used = max(step, atr_step_mult * max(atr, 1e-8))
    elif name == "Expanding":
        step_used = step * 1.15
    elif name == "Contracting":
        step_used = step * 0.85
    elif name == "Breakout":
        step_used = max(step, atr * 0.75)
    elif name == "RangeBound":
        step_used = max(step * 0.7, atr * 0.3)
    else:
        step_used = max(step, 1e-8)

    if name == "RegimeSwitchBasket":
        name = "ATRAdaptive" if atr > step * 2 else "Geometric"

    up, dn = levels_up, levels_down
    if name == "Inverse":
        up, dn = dn, up

    def lot_at(depth: int) -> float:
        lots = base_lots
        if name in ("Geometric", "MartingaleSoft"):
            lots *= lot_multiplier ** max(depth, 0)
        elif name == "AntiMartingale":
            lots *= lot_multiplier ** max(-depth, 0)
        elif name == "Fibonacci":
            lots *= _fib(depth) / 3.0
        else:
            lots *= 1.0 + 0.1 * depth
        max_lots = (equity * max_risk_frac) / max(step_used, 1e-8)
        return max(0.01, min(lots, max_lots))

    levels = []
    for i in range(1, dn + 1):
        spacing = _fib(i) * step_used if name == "Fibonacci" else step_used * i
        levels.append({"price": center - spacing, "lots": lot_at(i), "side": 1, "index": -i, "filled": False})
    for i in range(1, up + 1):
        spacing = _fib(i) * step_used if name == "Fibonacci" else step_used * i
        levels.append({"price": center + spacing, "lots": lot_at(i), "side": -1, "index": i, "filled": False})
    if name == "TrendFollowingGrid":
        for lv in levels:
            if lv["index"] < 0:
                lv["lots"] *= 1.25
    if name == "MeanReversionGrid":
        for lv in levels:
            lv["lots"] *= 1.1

    return {
        "type": name,
        "type_index": idx,
        "center": center,
        "step_used": step_used,
        "levels": levels,
        "floating_pnl": 0.0,
        "open_count": 0,
        "backend": "python",
    }
