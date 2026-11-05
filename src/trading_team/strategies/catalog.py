"""Strategy catalog — registry of all available quant strategies."""
from __future__ import annotations
from typing import Type

from .base import Strategy
from .trend_following import TrendFollowingStrategy
from .mean_reversion import MeanReversionStrategy
from .breakout import BreakoutStrategy
from .momentum_burst import MomentumBurstStrategy
from .session_liquidity import SessionLiquidityStrategy
from .ai_mesh_ensemble import AIMeshEnsembleStrategy
from .order_flow_continuation import OrderFlowContinuationStrategy
from .absorption_fade import AbsorptionFadeStrategy

# All strategies available to the system
STRATEGY_CATALOG: dict[str, Type[Strategy]] = {
    TrendFollowingStrategy.strategy_id: TrendFollowingStrategy,
    MeanReversionStrategy.strategy_id: MeanReversionStrategy,
    BreakoutStrategy.strategy_id: BreakoutStrategy,
    MomentumBurstStrategy.strategy_id: MomentumBurstStrategy,
    SessionLiquidityStrategy.strategy_id: SessionLiquidityStrategy,
    OrderFlowContinuationStrategy.strategy_id: OrderFlowContinuationStrategy,
    AbsorptionFadeStrategy.strategy_id: AbsorptionFadeStrategy,
    AIMeshEnsembleStrategy.strategy_id: AIMeshEnsembleStrategy,
}


def get_strategy(strategy_id: str) -> Strategy:
    cls = STRATEGY_CATALOG.get(strategy_id)
    if cls is None:
        raise KeyError(f"Unknown strategy_id: {strategy_id}. Available: {list(STRATEGY_CATALOG)}")
    return cls()


def list_strategies() -> list[dict]:
    return [cls().info() for cls in STRATEGY_CATALOG.values()]
