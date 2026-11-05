"""Mean Reversion — fade extremes in ranging / low-vol regimes."""
from __future__ import annotations
from statistics import mean, pstdev
from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr, closes, classify_regime, momentum_zscore, optimal_stop_distance,
)
from .base import Strategy, StrategySignal, StrategyContext


class MeanReversionStrategy(Strategy):
    strategy_id = "mean_reversion"
    name = "Statistical Mean Reversion"
    version = "1.0.0"
    description = "Fade z-score extremes when regime is RANGING and volatility is contained."
    preferred_trend = ("RANGING",)
    preferred_vol = ("LOW_VOL", "NORMAL_VOL")
    tags = ("mean-reversion", "range", "zscore")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        if len(m5) < 60:
            return StrategySignal(self.strategy_id, self.name, data_quality=0.2,
                                  rationale=["Insufficient history for mean-reversion"])

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5)
        fit = self.regime_fit(regime)
        cs = closes(m5)
        window = cs[-40:]
        mu = mean(window)
        sd = pstdev(window) or 1e-9
        z = (cs[-1] - mu) / sd
        mom_z = momentum_zscore(m5, 20)
        a = atr(m5, 14)

        rationale = [f"z={z:+.2f} mom_z={mom_z:+.2f} mean={mu:.2f}",
                     f"regime={regime.get('trend_regime')}/{regime.get('vol_regime')} fit={fit:.2f}"]

        # Only act on stretched prices in range-friendly regime
        if abs(z) < 1.6:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.30, 0.0,
                                  rationale=rationale + ["Price not stretched"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        if regime.get("trend_regime") == "TRENDING" and float(regime.get("strength") or 0) > 0.55:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.25, 0.0,
                                  rationale=rationale + ["Strong trend — do not fade"],
                                  tags=list(self.tags), regime_fit=fit * 0.4, data_quality=0.85)

        side = Side.SELL if z > 0 else Side.BUY  # fade
        conf = min(0.88, 0.45 + (abs(z) - 1.6) * 0.12 + (0.08 if abs(mom_z) > 1.5 else 0)) * fit
        score = conf if side == Side.BUY else -conf
        entry = snapshot.price
        # Tight stop beyond extreme; target near mean
        stop_d = max(a * 1.1, abs(cs[-1] - mu) * 0.35)
        stop = entry + stop_d if side == Side.SELL else entry - stop_d
        target = mu  # mean as primary target
        # Ensure minimum R:R
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk > 0 and reward / risk < 1.2:
            target = entry - risk * 1.5 if side == Side.SELL else entry + risk * 1.5

        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale + [f"FADE extreme z={z:+.2f} → {side.value}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.88,
        )
