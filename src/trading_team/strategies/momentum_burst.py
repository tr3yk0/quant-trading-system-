"""Momentum Burst — short-horizon velocity + volume spike."""
from __future__ import annotations
from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr, momentum_zscore, price_velocity, detect_volume_anomaly,
    classify_regime, optimal_stop_distance, optimal_target_distance, relative_volume,
)
from .base import Strategy, StrategySignal, StrategyContext


class MomentumBurstStrategy(Strategy):
    strategy_id = "momentum_burst"
    name = "Momentum Burst"
    version = "1.0.0"
    description = "Capture short-lived momentum bursts with volume confirmation; quick targets."
    preferred_trend = ("TRENDING", "TRANSITION")
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL")
    tags = ("momentum", "scalp", "volume")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        if len(m5) < 40:
            return StrategySignal(self.strategy_id, self.name, data_quality=0.2,
                                  rationale=["Insufficient history"])

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)
        mom_z = momentum_zscore(m5, 15)
        vel = price_velocity(m5, 6)
        anomaly = detect_volume_anomaly(m5)
        rvol = relative_volume(m5, 15)
        a = atr(m5, 14)

        rationale = [f"mom_z={mom_z:+.2f} vel={vel:+.4f} rvol={rvol:.1f}x",
                     f"anomaly={anomaly.get('anomaly')} confirmed={anomaly.get('price_confirmed')}",
                     f"regime fit={fit:.2f}"]

        strong = abs(mom_z) > 1.8 and abs(vel) > 0.0008
        vol_ok = rvol > 1.5 or anomaly.get("anomaly")
        if not strong or not vol_ok:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.28, 0.0,
                                  rationale=rationale + ["No momentum burst"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        side = Side.BUY if mom_z > 0 else Side.SELL
        conf = min(0.88, 0.50 + min(0.2, abs(mom_z) * 0.06) + (0.08 if anomaly.get("price_confirmed") else 0)) * fit
        score = conf if side == Side.BUY else -conf
        entry = snapshot.price
        stop_d = optimal_stop_distance(m5, side, atr_mult=1.2)
        tgt_d = stop_d * 1.6  # tighter R:R for burst
        stop = entry - stop_d if side == Side.BUY else entry + stop_d
        target = entry + tgt_d if side == Side.BUY else entry - tgt_d

        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"BURST {side.value}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.86,
        )
