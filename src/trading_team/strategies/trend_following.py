"""Trend Following — multi-timeframe momentum continuation."""
from __future__ import annotations
from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr, trend, trend_strength, momentum_score, multi_timeframe_confluence,
    classify_regime, optimal_stop_distance, optimal_target_distance,
)
from .base import Strategy, StrategySignal, StrategyContext


class TrendFollowingStrategy(Strategy):
    strategy_id = "trend_following"
    name = "Multi-TF Trend Following"
    version = "1.1.0"
    description = "Enter in direction of multi-timeframe trend when momentum confirms and regime is TRENDING."
    preferred_trend = ("TRENDING",)
    preferred_vol = ("NORMAL_VOL", "LOW_VOL")
    tags = ("trend", "momentum", "mtf")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        m15 = snapshot.candles.get("M15") or []
        h1 = snapshot.candles.get("H1") or []
        if len(m5) < 50:
            return StrategySignal(self.strategy_id, self.name, data_quality=0.2,
                                  rationale=["Insufficient M5 history"])

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)

        tf = {}
        if m5: tf["M5"] = trend(m5)
        if m15: tf["M15"] = trend(m15)
        if h1: tf["H1"] = trend(h1)
        mtf_side, mtf_conf, mtf_detail = multi_timeframe_confluence(tf)
        strength = trend_strength(m5)
        mom = momentum_score(m5, 20)

        rationale = [f"MTF={mtf_detail}", f"strength={strength:.2f} mom={mom:+.3f}",
                     f"regime={regime.get('trend_regime')}/{regime.get('vol_regime')} fit={fit:.2f}"]

        if mtf_side == Side.FLAT or mtf_conf < 0.50 or strength < 0.40:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.35, 0.0,
                                  rationale=rationale + ["No clear multi-TF trend"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        # Momentum must align
        if (mtf_side == Side.BUY and mom < 0.02) or (mtf_side == Side.SELL and mom > -0.02):
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.40, 0.0,
                                  rationale=rationale + ["Momentum does not confirm trend"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        conf = min(0.92, 0.50 + mtf_conf * 0.25 + strength * 0.15) * fit
        # Phase 2: soft order-flow confirmation
        try:
            from .order_flow_utils import extract_order_flow, flow_alignment
            flow = extract_order_flow(snapshot, ctx)
            mult, notes = flow_alignment(mtf_side, flow)
            conf = min(0.93, conf * mult)
            rationale.extend(notes)
        except Exception:
            pass
        score = conf if mtf_side == Side.BUY else -conf
        entry = snapshot.price
        stop_d = optimal_stop_distance(m5, mtf_side, atr_mult=1.5)
        tgt_d = optimal_target_distance(m5, stop_d, min_rr=2.0)
        stop = entry - stop_d if mtf_side == Side.BUY else entry + stop_d
        target = entry + tgt_d if mtf_side == Side.BUY else entry - tgt_d

        return StrategySignal(
            self.strategy_id, self.name, mtf_side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"ENTRY trend-follow {mtf_side.value}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.9,
        )
