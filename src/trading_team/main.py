"""
AI Trading Team — single-cycle entry point.
Supports paper (default) and live MT5 execution when LIVE_TRADING_ENABLED=true
and TRADING_MODE=live with arming handled by the dashboard/supervisor.
"""
from __future__ import annotations
import os
from trading_team.core.config import AppConfig
from trading_team.ai_mesh import AgentMesh
from trading_team.agents.risk import RiskAgent
from trading_team.agents.decision import DecisionAgent
from trading_team.agents.team import run_specialist_team
from trading_team.storage.journal import Journal
from trading_team.data.mt5 import MT5DataFeed, MT5Unavailable
from trading_team.data.mtf import MultiTimeframeFeed
from trading_team.storage.state import AccountState
from trading_team.research.adaptive_confidence import AdaptiveConfidenceEngine


def analyze(
    snapshot,
    cfg,
    equity=10000,
    daily_pnl=0,
    exposure=0,
    open_positions=0,
    force_ai=False,
    use_specialists=True,
):
    """
    Full analysis cycle.
    - Primary path: AgentMesh (research debate + risk debate)
    - Parallel specialist stack (8 agents including Regime + Portfolio)
    - DecisionAgent with adaptive confidence feedback
    - RiskAgent hard gate
    """
    mesh = AgentMesh()
    result = mesh.analyze(snapshot, force_ai=force_ai)
    signals, packet = mesh.to_legacy(result, snapshot.symbol)

    # Enrich with dedicated specialist team (Regime as 7th + Portfolio)
    account_meta = {
        "equity": equity,
        "daily_pnl": daily_pnl,
        "exposure": exposure,
        "open_positions": open_positions,
        "max_positions": cfg.risk.max_positions,
        "max_exposure_pct": cfg.risk.max_account_exposure_pct,
        "max_daily_loss_pct": cfg.risk.max_daily_loss_pct,
    }
    if use_specialists:
        try:
            specialist_signals = run_specialist_team(snapshot, account_meta=account_meta)
            # Merge: keep mesh signals, append specialists not already present
            existing = {s.agent for s in signals}
            for s in specialist_signals:
                if s.agent not in existing:
                    signals.append(s)
            # Re-synthesize with full ensemble + adaptive confidence
            decision_agent = DecisionAgent(AdaptiveConfidenceEngine())
            packet = decision_agent.synthesize(signals, snapshot)
        except Exception as exc:
            # Fail-open to mesh packet; log the specialist failure
            from trading_team.core.models import AgentSignal
            signals.append(AgentSignal(
                agent="TEAM",
                symbol=snapshot.symbol,
                evidence=[f"Specialist team error: {exc}"],
                data_quality=0.2,
            ))

    risk = None
    execution = None
    if packet.plan:
        risk = RiskAgent(cfg.risk).evaluate(
            snapshot, packet.plan, equity, daily_pnl, exposure, open_positions
        )
        if risk.approved:
            if cfg.paper_trading or not cfg.live_trading_enabled:
                execution = {"status": "PAPER_HOLD", "reason": "Paper mode or live disabled"}
            else:
                # Live path is intentionally gated; actual order submission
                # goes through the dashboard Runtime which enforces arm/kill-switch.
                execution = {
                    "status": "LIVE_READY",
                    "reason": "Risk approved — submit via armed Runtime / MT5ExecutionAdapter",
                    "quantity": risk.quantity,
                }
        else:
            execution = {"status": "RISK_BLOCKED", "reason": risk.reason}
    else:
        execution = {
            "status": "NO_PLAN",
            "reason": "AI agent mesh / specialists did not authorize an actionable setup",
        }
    return signals, packet, risk, execution, result


def run_once():
    cfg = AppConfig.from_env()
    feed = None
    try:
        feed = MT5DataFeed()
        snapshot = MultiTimeframeFeed(feed).snapshot(
            cfg.symbol, ("M5", "M15", "H1"), cfg.lookback
        )
        state = AccountState()
        acct = state.load() or {
            "equity": 10000,
            "daily_pnl": 0,
            "exposure": 0,
            "open_positions": 0,
        }
        signals, packet, risk, execution, mesh = analyze(
            snapshot,
            cfg,
            acct["equity"],
            acct["daily_pnl"],
            acct["exposure"],
            acct["open_positions"],
        )
        Journal().write(
            {
                "type": "ai_mesh_cycle",
                "symbol": snapshot.symbol,
                "decision": packet.decision.value,
                "confidence": packet.confidence,
                "signals": [s.__dict__ for s in signals if s],
                "risk": risk.__dict__ if risk else None,
                "execution": execution,
                "mesh": mesh.to_dict() if hasattr(mesh, "to_dict") else str(mesh),
            }
        )
        print(f"{snapshot.symbol} {packet.decision.value} confidence={packet.confidence:.2f}")
        if risk:
            print(f"RISK {'APPROVE' if risk.approved else 'REJECT'}: {risk.reason}")
        print(f"EXECUTION: {execution}")
    except MT5Unavailable as e:
        print(f"MT5 unavailable: {e}")
    finally:
        if feed:
            feed.shutdown()


if __name__ == "__main__":
    run_once()
