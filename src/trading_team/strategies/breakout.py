"""Volatility Breakout — range expansion with volume confirmation."""
from __future__ import annotations
from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr, highs, lows, closes, classify_regime, detect_volume_anomaly,
    relative_volume, optimal_stop_distance, optimal_target_distance, trend_strength,
)
from .base import Strategy, StrategySignal, StrategyContext


class BreakoutStrategy(Strategy):
    strategy_id = "breakout"
    name = "Volatility Breakout"
    version = "1.0.0"
    description = "Trade range breakouts confirmed by volume surge and expanding ATR."
    preferred_trend = ("TRANSITION", "TRENDING")
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL")
    tags = ("breakout", "volatility", "volume")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        if len(m5) < 50:
            return StrategySignal(self.strategy_id, self.name, data_quality=0.2,
                                  rationale=["Insufficient history for breakout"])

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)
        lookback = 20
        hh = max(highs(m5)[-lookback - 1:-1])  # prior range high (exclude current)
        ll = min(lows(m5)[-lookback - 1:-1])
        px = closes(m5)[-1]
        a = atr(m5, 14)
        anomaly = detect_volume_anomaly(m5)
        rvol = relative_volume(m5, 20)
        strength = trend_strength(m5)

        range_w = hh - ll
        rationale = [f"range=[{ll:.2f},{hh:.2f}] px={px:.2f} width={range_w:.2f}",
                     f"rvol={rvol:.1f}x anomaly={anomaly.get('anomaly')} strength={strength:.2f}",
                     f"regime={regime.get('trend_regime')}/{regime.get('vol_regime')} fit={fit:.2f}"]

        broke_up = px > hh and (px - hh) > a * 0.15
        broke_dn = px < ll and (ll - px) > a * 0.15
        vol_ok = rvol > 1.4 or anomaly.get("anomaly")

        if not (broke_up or broke_dn) or not vol_ok:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.30, 0.0,
                                  rationale=rationale + ["No confirmed breakout"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        side = Side.BUY if broke_up else Side.SELL
        conf = min(0.90, 0.48 + (0.12 if anomaly.get("price_confirmed") else 0.05)
                   + min(0.15, (rvol - 1.0) * 0.08) + strength * 0.10) * fit
        # Phase 2: block/penalize breakouts under opposing CVD absorption
        try:
            from .order_flow_utils import extract_order_flow, flow_alignment
            flow = extract_order_flow(snapshot, ctx)
            div = str(flow.get("divergence") or "none")
            if side == Side.BUY and div == "bearish_absorption":
                return StrategySignal(
                    self.strategy_id, self.name, Side.FLAT, 0.32, 0.0,
                    rationale=rationale + ["Breakout long blocked — bearish CVD absorption"],
                    tags=list(self.tags), regime_fit=fit, data_quality=0.88,
                )
            if side == Side.SELL and div == "bullish_absorption":
                return StrategySignal(
                    self.strategy_id, self.name, Side.FLAT, 0.32, 0.0,
                    rationale=rationale + ["Breakout short blocked — bullish CVD absorption"],
                    tags=list(self.tags), regime_fit=fit, data_quality=0.88,
                )
            mult, notes = flow_alignment(side, flow)
            conf = min(0.92, conf * mult)
            rationale.extend(notes)
        except Exception:
            pass
        score = conf if side == Side.BUY else -conf
        entry = snapshot.price
        stop_d = optimal_stop_distance(m5, side, atr_mult=1.3)
        # Stop inside broken range
        if side == Side.BUY:
            stop = max(entry - stop_d, ll)
            target = entry + optimal_target_distance(m5, abs(entry - stop), min_rr=1.8)
        else:
            stop = min(entry + stop_d, hh)
            target = entry - optimal_target_distance(m5, abs(stop - entry), min_rr=1.8)

        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"BREAKOUT {side.value} vol-confirmed"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.88,
        )
