"""Session Liquidity Sweep — trade post-liquidity-grab reversals / continuations."""
from __future__ import annotations
from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import (
    atr, highs, lows, closes, classify_regime, relative_volume,
    optimal_stop_distance, session_quality_score, trend,
)
from trading_team.analysis.structure import analyze_structure
from .base import Strategy, StrategySignal, StrategyContext


class SessionLiquidityStrategy(Strategy):
    strategy_id = "session_liquidity"
    name = "Session Liquidity Sweep"
    version = "1.0.0"
    description = "Detect liquidity sweeps (stop hunts) at swing highs/lows and trade the reclaim."
    preferred_trend = ("RANGING", "TRANSITION", "TRENDING")
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL")
    tags = ("liquidity", "structure", "session")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        m5 = snapshot.candles.get("M5") or []
        m15 = snapshot.candles.get("M15") or []
        primary = m15 if len(m15) >= 40 else m5
        if len(primary) < 40:
            return StrategySignal(self.strategy_id, self.name, data_quality=0.2,
                                  rationale=["Insufficient structure history"])

        regime = (ctx.regime if ctx and ctx.regime else None) or classify_regime(m5 if m5 else primary)
        fit = self.regime_fit(regime)
        struct = analyze_structure(primary)
        a = atr(m5 if m5 else primary, 14)
        quality = session_quality_score(snapshot.spread, snapshot.price, a) if a > 0 else 0.5
        rvol = relative_volume(m5 if m5 else primary, 20)
        px = snapshot.price
        t = trend(m5 if m5 else primary)

        rationale = [
            f"swing_h={struct.swing_high} swing_l={struct.swing_low}",
            f"quality={quality:.2f} rvol={rvol:.1f}x trend={t.value}",
            f"regime fit={fit:.2f}",
        ]

        if quality < 0.35:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.2, 0.0,
                                  rationale=rationale + ["Poor session quality"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.7)

        side = Side.FLAT
        conf = 0.35
        # Sweep above swing high then reclaim below → short bias (liquidity grab)
        if struct.swing_high and px < struct.swing_high and (struct.swing_high - px) < a * 1.5:
            # recent high pierced then closed back
            recent_h = max(highs(m5)[-8:]) if m5 else max(highs(primary)[-6:])
            if recent_h > struct.swing_high + a * 0.05 and px < struct.swing_high:
                side = Side.SELL
                conf = min(0.85, 0.52 + (0.1 if rvol > 1.3 else 0) + (0.08 if t == Side.SELL else 0)) * fit
                rationale.append("Liquidity sweep above swing high → reclaim short")
        # Sweep below swing low then reclaim above → long bias
        if struct.swing_low and px > struct.swing_low and (px - struct.swing_low) < a * 1.5:
            recent_l = min(lows(m5)[-8:]) if m5 else min(lows(primary)[-6:])
            if recent_l < struct.swing_low - a * 0.05 and px > struct.swing_low:
                side = Side.BUY
                conf = min(0.85, 0.52 + (0.1 if rvol > 1.3 else 0) + (0.08 if t == Side.BUY else 0)) * fit
                rationale.append("Liquidity sweep below swing low → reclaim long")

        if side == Side.FLAT:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.30, 0.0,
                                  rationale=rationale + ["No liquidity sweep pattern"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        score = conf if side == Side.BUY else -conf
        entry = px
        stop_d = optimal_stop_distance(m5 if m5 else primary, side, atr_mult=1.4)
        if side == Side.BUY:
            stop = min(entry - stop_d, (struct.swing_low or entry) - a * 0.2)
            target = entry + abs(entry - stop) * 2.0
        else:
            stop = max(entry + stop_d, (struct.swing_high or entry) + a * 0.2)
            target = entry - abs(stop - entry) * 2.0

        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(score, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=rationale, tags=list(self.tags), regime_fit=fit, data_quality=0.87,
        )
