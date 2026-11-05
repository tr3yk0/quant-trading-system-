"""Absorption Fade — fade exhausted breakouts when CVD diverges from price."""
from __future__ import annotations

from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    classify_regime,
    momentum_score,
    optimal_stop_distance,
    optimal_target_distance,
    support_resistance,
    trend_strength,
)
from .base import Strategy, StrategyContext, StrategySignal
from .order_flow_utils import extract_order_flow, flow_alignment, spread_cost_score


class AbsorptionFadeStrategy(Strategy):
    strategy_id = "absorption_fade"
    name = "CVD Absorption Fade"
    version = "2.0.0"
    description = (
        "Phase 2: When price presses a range extreme but CVD/OBI show opposing absorption, "
        "fade the move (mean-revert) with tight risk."
    )
    preferred_trend = ("RANGING", "TRENDING")
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL")
    tags = ("order_flow", "cvd", "absorption", "mean_reversion", "phase2")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        if len(m5) < 40:
            return StrategySignal(
                self.strategy_id, self.name, data_quality=0.2,
                rationale=["Insufficient M5 history"],
            )

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)
        flow = extract_order_flow(snapshot, ctx)
        cost, cost_note = spread_cost_score(snapshot, soft_bps=15.0, hard_bps=35.0)
        strength = trend_strength(m5)
        mom = momentum_score(m5, 14)
        support, resistance = support_resistance(m5, 30)
        px = float(snapshot.price)
        width = max(resistance - support, 1e-9)
        pos = (px - support) / width

        rationale = [
            f"range pos={pos:.2f} support={support:.5g} resist={resistance:.5g}",
            f"mom={mom:+.3f} strength={strength:.2f} regime={regime.get('trend_regime')}",
            f"divergence={flow.get('divergence')} proxy={flow.get('proxy'):+.3f}",
            cost_note,
        ]

        if not flow.get("available"):
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.28, 0.0,
                rationale=rationale + ["No order-flow — fade strategy inactive"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.5,
            )
        if cost <= 0.2:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.22, 0.0,
                rationale=rationale + ["Spread too wide to fade"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.8,
            )

        div = str(flow.get("divergence") or "none")
        side = Side.FLAT
        # Price near highs + bearish absorption → fade short
        if pos >= 0.82 and (div == "bearish_absorption" or (mom > 0 and float(flow.get("cvd_slope") or 0) < 0)):
            side = Side.SELL
            rationale.append("High-range + bearish CVD absorption → fade short")
        # Price near lows + bullish absorption → fade long
        elif pos <= 0.18 and (div == "bullish_absorption" or (mom < 0 and float(flow.get("cvd_slope") or 0) > 0)):
            side = Side.BUY
            rationale.append("Low-range + bullish CVD absorption → fade long")
        else:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.34, 0.0,
                rationale=rationale + ["No absorption extreme setup"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.85,
            )

        align_mult, align_notes = flow_alignment(side, flow)
        rationale.extend(align_notes)
        # For fades, opposing flow to the *prior* thrust is the edge; align_mult already handles absorption
        conf = min(0.88, 0.50 + abs(float(flow.get("proxy") or 0)) * 0.2 + (0.1 if div.endswith("absorption") else 0.0))
        conf *= fit * max(0.6, align_mult) * max(0.5, cost)
        if conf < 0.48:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, round(conf, 3), 0.0,
                rationale=rationale + ["Confidence below fade threshold"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.86,
            )

        entry = px
        stop_d = optimal_stop_distance(m5, side, atr_mult=1.15)
        tgt_d = optimal_target_distance(m5, stop_d, min_rr=1.5)
        # Prefer target toward mid-range
        mid = (support + resistance) / 2.0
        if side == Side.SELL:
            stop = entry + stop_d
            target = min(entry - tgt_d, mid) if mid < entry else entry - tgt_d
        else:
            stop = entry - stop_d
            target = max(entry + tgt_d, mid) if mid > entry else entry + tgt_d

        score = conf if side == Side.BUY else -conf
        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"ENTRY absorption fade {side.value}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.9,
        )
