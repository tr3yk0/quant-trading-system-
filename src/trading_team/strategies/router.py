"""
Strategy Router — regime-aware strategy selection and ensemble.
Evaluates all (or a subset of) strategies, scores by regime fit × confidence,
and returns the best signal plus a full audit of competitors.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from trading_team.core.models import MarketSnapshot, Side
from trading_team.indicators.technical import classify_regime

from .base import Strategy, StrategySignal, StrategyContext
from .catalog import STRATEGY_CATALOG, get_strategy
from trading_team.research.regime_strategy_map import (
    strategy_ids_for_regime, confidence_multiplier, get_policy,
)
from trading_team.research.regime_params import normalize_regime_key


@dataclass
class RouterResult:
    selected: StrategySignal | None
    candidates: list[StrategySignal] = field(default_factory=list)
    regime: dict[str, Any] = field(default_factory=dict)
    selection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "regime": {k: (v.value if hasattr(v, "value") else v) for k, v in self.regime.items()},
            "selection_reason": self.selection_reason,
        }


class StrategyRouter:
    """
    Evaluates strategies and picks the best regime-aligned signal.
    Default: all catalog strategies except ai_mesh_ensemble (which is the
    agent pipeline itself). Callers can pass strategy_ids to restrict.
    """

    def __init__(
        self,
        strategy_ids: list[str] | None = None,
        min_confidence: float = 0.52,
        min_regime_fit: float = 0.45,
    ):
        if strategy_ids is None:
            strategy_ids = [sid for sid in STRATEGY_CATALOG if sid != "ai_mesh_ensemble"]
        self.strategy_ids = list(strategy_ids)
        self.min_confidence = min_confidence
        self.min_regime_fit = min_regime_fit

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        ctx: StrategyContext | None = None,
    ) -> RouterResult:
        m5 = snapshot.candles.get("M5") or []
        regime = {}
        if ctx and ctx.regime:
            regime = ctx.regime
        elif len(m5) >= 40:
            regime = classify_regime(m5)

        # Phase-1 order-flow flags for strategy routing (absorption / exhaustion)
        meta = dict(snapshot.metadata or {})
        of_detail = meta.get("order_flow_detail") if isinstance(meta.get("order_flow_detail"), dict) else {}
        of_flags = list(of_detail.get("flags") or [])
        div = of_detail.get("divergence") if isinstance(of_detail.get("divergence"), dict) else {}
        if div.get("divergence") and div.get("divergence") != "none":
            of_flags.append(str(div["divergence"]))
        regime = dict(regime)
        regime["order_flow_proxy"] = float(of_detail.get("order_flow_proxy") or meta.get("order_flow") or 0.0)
        regime["obi"] = float((of_detail.get("obi") or {}).get("obi") or 0.0)
        regime["cvd_slope"] = float((of_detail.get("cvd") or {}).get("cvd_slope") or 0.0)
        regime["order_flow_flags"] = of_flags
        regime["cvd_divergence"] = str(div.get("divergence") or "none")

        # ── Regime-conditional strategy set (v4.11) ─────────────────
        regime_key = "UNKNOWN"
        for key in ("hmm_state", "trend_regime", "regime", "state"):
            if regime.get(key):
                regime_key = normalize_regime_key(regime.get(key))
                if regime_key != "UNKNOWN":
                    break
        # Also map classic classify_regime labels
        tr = str(regime.get("trend_regime") or regime.get("label") or "").upper()
        if regime_key == "UNKNOWN":
            if "UP" in tr or tr == "TRENDING":
                # direction from side/score if present
                direction = str(regime.get("direction") or "").upper()
                if direction in ("BUY", "UP", "BULL"):
                    regime_key = "TREND_UP"
                elif direction in ("SELL", "DOWN", "BEAR"):
                    regime_key = "TREND_DOWN"
                else:
                    regime_key = "TREND_UP" if "UP" in tr else "UNKNOWN"
            elif "RANGE" in tr or "SIDE" in tr or "CHOP" in tr:
                regime_key = "RANGE"
            elif "HIGH" in tr or "VOL" in tr:
                regime_key = "HIGH_VOL"
        vol_r = str(regime.get("vol_regime") or "").upper()
        if vol_r in ("HIGH", "EXTREME", "CRISIS") and regime_key not in ("CRISIS",):
            if vol_r == "CRISIS" or "CRISIS" in vol_r:
                regime_key = "CRISIS"
            elif regime_key == "UNKNOWN":
                regime_key = "HIGH_VOL"
        regime["regime_key"] = regime_key
        policy = get_policy(regime_key)
        active_ids = strategy_ids_for_regime(regime_key, catalog_ids=self.strategy_ids)
        if not active_ids:
            active_ids = list(self.strategy_ids)

        # Build context with regime
        base_ctx = StrategyContext(
            equity=ctx.equity if ctx else 10000.0,
            daily_pnl=ctx.daily_pnl if ctx else 0.0,
            exposure=ctx.exposure if ctx else 0.0,
            open_positions=ctx.open_positions if ctx else 0,
            regime=regime,
            metadata={
                **(dict(ctx.metadata) if ctx and ctx.metadata else {}),
                "order_flow_detail": of_detail,
                "order_flow_flags": of_flags,
            },
        )

        candidates: list[StrategySignal] = []
        for sid in active_ids:
            try:
                strat = get_strategy(sid)
                sig = strat.evaluate(snapshot, base_ctx)
                candidates.append(sig)
            except Exception as exc:
                candidates.append(StrategySignal(
                    strategy_id=sid, name=sid, side=Side.FLAT, confidence=0.0,
                    rationale=[f"Strategy error: {exc}"], data_quality=0.1,
                ))

        # Regime preferred-strategy confidence boost / block penalty
        boosted: list[StrategySignal] = []
        for c in candidates:
            mult = confidence_multiplier(c.strategy_id, regime.get("regime_key", "UNKNOWN"))
            new_conf = float(c.confidence or 0.0) * float(mult)
            new_fit = float(getattr(c, "regime_fit", 0.5) or 0.5)
            extra = []
            if mult <= 0.0:
                new_conf = 0.0
                new_fit = 0.0
                extra.append(f"blocked in regime {regime.get('regime_key')}")
            elif mult != 1.0:
                new_conf = min(0.95, new_conf)
                extra.append(f"regime_boost x{mult:.2f}")
            try:
                from dataclasses import replace
                c = replace(
                    c,
                    confidence=new_conf,
                    regime_fit=new_fit,
                    rationale=list(c.rationale or []) + extra,
                )
            except Exception:
                pass
            boosted.append(c)
        candidates = boosted

        # Order-flow soft filters: mute breakouts under opposing absorption
        adjusted: list[StrategySignal] = []
        for c in candidates:
            conf = float(c.confidence)
            rationale = list(c.rationale or [])
            if c.side == Side.BUY and "bearish_absorption" in of_flags:
                conf *= 0.55
                rationale.append("CVD bearish absorption soft-penalized long signal")
            elif c.side == Side.SELL and "bullish_absorption" in of_flags:
                conf *= 0.55
                rationale.append("CVD bullish absorption soft-penalized short signal")
            elif c.side == Side.BUY and (
                "bullish_confirm" in of_flags or "cvd_slope_up" in of_flags or "obi_pressure_bid" in of_flags
            ):
                conf = min(0.95, conf * 1.08)
                rationale.append("Order-flow supports long (CVD/OBI)")
            elif c.side == Side.SELL and (
                "bearish_confirm" in of_flags or "cvd_slope_down" in of_flags or "obi_pressure_ask" in of_flags
            ):
                conf = min(0.95, conf * 1.08)
                rationale.append("Order-flow supports short (CVD/OBI)")
            if conf != float(c.confidence) or rationale != list(c.rationale or []):
                c = StrategySignal(
                    strategy_id=c.strategy_id,
                    name=c.name,
                    side=c.side,
                    confidence=round(conf, 4),
                    score=float(getattr(c, "score", 0.0) or 0.0),
                    regime_fit=c.regime_fit,
                    entry=c.entry,
                    stop=c.stop,
                    target=c.target,
                    rationale=rationale,
                    tags=list(getattr(c, "tags", None) or []),
                    data_quality=c.data_quality,
                )
            adjusted.append(c)
        candidates = adjusted

        # Phase 2 multi-factor rank: confidence × regime_fit × flow_align × cost
        from .order_flow_utils import spread_cost_score
        cost_score, cost_note = spread_cost_score(snapshot)
        proxy = float(regime.get("order_flow_proxy") or 0.0)

        def _flow_factor(sig: StrategySignal) -> float:
            if sig.side == Side.BUY:
                if proxy > 0.05 or "bullish_confirm" in of_flags or "cvd_slope_up" in of_flags:
                    return 1.08
                if "bearish_absorption" in of_flags or proxy < -0.08:
                    return 0.60
            if sig.side == Side.SELL:
                if proxy < -0.05 or "bearish_confirm" in of_flags or "cvd_slope_down" in of_flags:
                    return 1.08
                if "bullish_absorption" in of_flags or proxy > 0.08:
                    return 0.60
            return 1.0

        def _rank(sig: StrategySignal) -> float:
            return (
                float(sig.confidence)
                * float(sig.regime_fit)
                * _flow_factor(sig)
                * max(0.35, cost_score)
            )

        # Rank actionable signals by Phase-2 multi-factor score
        actionable = [
            c for c in candidates
            if c.side != Side.FLAT
            and c.confidence >= self.min_confidence
            and c.regime_fit >= self.min_regime_fit
            and c.entry is not None
        ]
        actionable.sort(key=_rank, reverse=True)

        if not actionable:
            return RouterResult(
                selected=None,
                candidates=candidates,
                regime=regime,
                selection_reason="No strategy met confidence/regime-fit thresholds",
            )

        best = actionable[0]
        rank = _rank(best)
        reason = (
            f"Selected {best.name} ({best.strategy_id}) "
            f"side={best.side.value} conf={best.confidence:.2f} fit={best.regime_fit:.2f} "
            f"rank={rank:.3f} cost={cost_score:.2f} "
            f"among {len(actionable)} actionable / {len(candidates)} evaluated — {cost_note}"
        )
        return RouterResult(
            selected=best,
            candidates=candidates,
            regime=regime,
            selection_reason=reason,
        )
