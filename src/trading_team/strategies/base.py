"""
Strategy base types.
A Strategy produces a StrategySignal from a MarketSnapshot + optional context.
Strategies are pure (no side effects); Decision / Risk / Execution remain authoritative.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from trading_team.core.models import MarketSnapshot, Side, TradePlan


@dataclass
class StrategyContext:
    """Optional runtime context passed to strategies."""
    equity: float = 10000.0
    daily_pnl: float = 0.0
    exposure: float = 0.0
    open_positions: int = 0
    regime: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategySignal:
    strategy_id: str
    name: str
    side: Side = Side.FLAT
    confidence: float = 0.0
    score: float = 0.0
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    rationale: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    regime_fit: float = 1.0  # 0–1 how well current regime matches strategy design
    data_quality: float = 1.0

    def to_trade_plan(self, symbol: str) -> TradePlan | None:
        if self.side == Side.FLAT or self.entry is None or self.stop is None or self.target is None:
            return None
        return TradePlan(
            symbol=symbol,
            side=self.side,
            entry=float(self.entry),
            stop=float(self.stop),
            target=float(self.target),
            confidence=float(self.confidence),
            rationale=list(self.rationale),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "side": self.side.value,
            "confidence": self.confidence,
            "score": self.score,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rationale": self.rationale,
            "tags": self.tags,
            "regime_fit": self.regime_fit,
            "data_quality": self.data_quality,
        }


class Strategy(ABC):
    """Base class for all quant strategies."""
    strategy_id: str = "base"
    name: str = "Base Strategy"
    version: str = "1.0.0"
    description: str = ""
    # Preferred regimes (trend_regime, vol_regime) — used by router for fit scoring
    preferred_trend: tuple[str, ...] = ("TRENDING", "TRANSITION", "RANGING")
    preferred_vol: tuple[str, ...] = ("NORMAL_VOL", "HIGH_VOL", "LOW_VOL")
    tags: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> StrategySignal:
        raise NotImplementedError

    def regime_fit(self, regime: dict[str, Any]) -> float:
        """0–1 score of how well current regime matches strategy design."""
        if not regime:
            return 0.5
        fit = 1.0
        tr = regime.get("trend_regime", "UNKNOWN")
        vr = regime.get("vol_regime", "UNKNOWN")
        if tr not in self.preferred_trend:
            fit *= 0.55
        if vr not in self.preferred_vol:
            fit *= 0.70
        return round(fit, 3)

    def info(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "preferred_trend": list(self.preferred_trend),
            "preferred_vol": list(self.preferred_vol),
            "tags": list(self.tags),
        }
