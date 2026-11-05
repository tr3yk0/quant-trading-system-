"""AI Mesh Ensemble — wraps agent ensemble output as a first-class strategy."""
from __future__ import annotations
from trading_team.core.models import MarketSnapshot, Side, AgentSignal
from trading_team.indicators.technical import classify_regime, optimal_stop_distance, optimal_target_distance
from .base import Strategy, StrategySignal, StrategyContext


class AIMeshEnsembleStrategy(Strategy):
    strategy_id = "ai_mesh_ensemble"
    name = "AI Mesh Ensemble"
    version = "3.0.0"
    description = "Regime-aware ensemble of specialist agents (default system strategy)."
    preferred_trend = ("TRENDING", "TRANSITION", "RANGING")
    preferred_vol = ("NORMAL_VOL", "HIGH_VOL", "LOW_VOL")
    tags = ("ensemble", "ai", "multi-agent")

    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        """
        When agent signals are supplied via ctx.metadata['agent_signals'],
        synthesize a strategy signal from them. Otherwise returns FLAT
        (agents are evaluated elsewhere in the pipeline).
        """
        m5 = snapshot.candles.get("M5") or []
        regime = (ctx.regime if ctx and ctx.regime else None) or (
            classify_regime(m5) if len(m5) >= 40 else {}
        )
        fit = self.regime_fit(regime)
        signals: list[AgentSignal] = []
        if ctx and ctx.metadata:
            signals = list(ctx.metadata.get("agent_signals") or [])

        if not signals:
            return StrategySignal(
                self.strategy_id, self.name, Side.FLAT, 0.0, 0.0,
                rationale=["No agent signals supplied — ensemble runs in main pipeline"],
                tags=list(self.tags), regime_fit=fit, data_quality=0.5,
            )

        # Simple weighted vote (mirrors DecisionAgent core logic)
        weights = {
            "SCOUT": 0.16, "STRUCTURE": 0.18, "CHARTS": 0.15, "REGIME": 0.14,
            "NEWS": 0.12, "SENTIMENT": 0.10, "MICROSTRUCTURE": 0.08,
        }
        score = 0.0
        tw = 0.0
        reasons = []
        for s in signals:
            if s.agent == "PORTFOLIO":
                continue
            w = weights.get(s.agent, 0.10) * max(0.3, s.data_quality)
            tw += w
            if s.side == Side.BUY:
                score += w * s.confidence
            elif s.side == Side.SELL:
                score -= w * s.confidence
            if s.evidence:
                reasons.append(f"[{s.agent}] {s.evidence[0]}")

        if tw <= 0:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, 0.0, 0.0,
                                  rationale=["Zero weight signals"], tags=list(self.tags),
                                  regime_fit=fit, data_quality=0.5)

        ensemble = score / tw
        conf = min(0.95, abs(ensemble) + 0.12) * fit
        if abs(ensemble) < 0.18:
            return StrategySignal(self.strategy_id, self.name, Side.FLAT, round(conf, 3),
                                  round(ensemble, 4), rationale=reasons[:6] + ["Below ensemble threshold"],
                                  tags=list(self.tags), regime_fit=fit, data_quality=0.85)

        side = Side.BUY if ensemble > 0 else Side.SELL
        entry = snapshot.price
        stop_d = optimal_stop_distance(m5, side) if m5 else entry * 0.002
        tgt_d = optimal_target_distance(m5, stop_d) if m5 else stop_d * 2.0
        stop = entry - stop_d if side == Side.BUY else entry + stop_d
        target = entry + tgt_d if side == Side.BUY else entry - tgt_d

        return StrategySignal(
            self.strategy_id, self.name, side, round(conf, 3), round(ensemble, 4),
            entry=round(entry, 5), stop=round(stop, 5), target=round(target, 5),
            rationale=reasons[:6] + [f"ENSEMBLE {side.value} score={ensemble:+.3f}"],
            tags=list(self.tags), regime_fit=fit, data_quality=0.9,
        )
