"""Order-Flow Continuation — trade with MTF trend when OBI/CVD confirm aggression."""
from __future__ import annotations

from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr,
    classify_regime,
    momentum_score,
    multi_timeframe_confluence,
    optimal_stop_distance,
    optimal_target_distance,
    trend,
    trend_strength,
)
from .base import Strategy, StrategyContext, StrategySignal
from .order_flow_utils import extract_order_flow, flow_alignment, spread_cost_score


class OrderFlowContinuationStrategy(Strategy):
    strategy_id = "order_flow_continuation"
    name = "Order-Flow Continuation"
    version = "2.0.0"
    description = (
        "Phase 2: Enter with multi-timeframe trend only when tick-level CVD slope "
        "and/or DOM imbalance (OBI) confirm aggressive flow in the same direction."
    )
    preferred_trend = ("TRENDING",)
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL", "LOW_VOL")
    tags = ("order_flow", "cvd", "obi", "trend", "phase2")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        m15 = snapshot.candles.get("M15") or []
        h1 = snapshot.candles.get("H1") or []
        if len(m5) < 50:
            return StrategySignal(
                self.strategy_id, self.name, data_quality=0.2,
                rationale=["Insufficient M5 history"],
            )

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)
        flow = extract_order_flow(snapshot, ctx)
        cost, cost_note = spread_cost_score(snapshot)

        tf = {}
        if m5:
            tf["M5"] = trend(m5)
        if m15:
            tf["M15"] = trend(m15)
        if h1:
            tf["H1"] = trend(h1)
        mtf_side, mtf_conf, mtf_detail = multi_timeframe_confluence(tf)
        strength = trend_strength(m5)
        mom = momentum_score(m5, 20)

        rationale = [
            f"MTF={mtf_detail}",
            f"strength={strength:.2f} mom={mom:+.3f}",
            f"regime fit={fit:.2f}",
            f"flow status={flow.get('status')} proxy={flow.get('proxy'):+.3f} "
            f"OBI={flow.get('obi'):+.3f} CVD_slope={flow.get('cvd_slope'):+.3f}",
            cost_note,
        ]

        if not flow.get("available"):
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.30, 0.0,
                rationale=rationale + ["No tick/DOM order-flow — strategy stands down"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.55,
            )
        if cost <= 0.15:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.20, 0.0,
                rationale=rationale + ["Spread cost too high for flow continuation"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.8,
            )
        if mtf_side == Side.FLAT or mtf_conf < 0.48 or strength < 0.35:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.35, 0.0,
                rationale=rationale + ["No clear multi-TF trend for continuation"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.85,
            )

        # Require flow agreement with trend
        align_mult, align_notes = flow_alignment(mtf_side, flow)
        rationale.extend(align_notes)
        if align_mult < 0.85:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.38, 0.0,
                rationale=rationale + ["Order-flow does not confirm trend direction"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.88,
            )

        conf = min(0.93, (0.48 + mtf_conf * 0.22 + strength * 0.12 + abs(float(flow["proxy"])) * 0.15))
        conf *= fit * align_mult * max(0.55, cost)
        score = conf if mtf_side == Side.BUY else -conf
        entry = snapshot.price
        stop_d = optimal_stop_distance(m5, mtf_side, atr_mult=1.4)
        tgt_d = optimal_target_distance(m5, stop_d, min_rr=1.8)
        stop = entry - stop_d if mtf_side == Side.BUY else entry + stop_d
        target = entry + tgt_d if mtf_side == Side.BUY else entry - tgt_d

        return StrategySignal(
            self.strategy_id, self.name, mtf_side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"ENTRY order-flow continuation {mtf_side.value}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.92,
        )
