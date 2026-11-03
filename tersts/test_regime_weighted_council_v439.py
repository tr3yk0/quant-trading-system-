from datetime import datetime, timedelta, timezone

from trading_team.core.models import AgentSignal, Candle, MarketSnapshot, Side, TradePlan
from trading_team.ai_mesh.models import AgentReport
from trading_team.market.scout_scanner import ScoutConfig, ScoutScanner
from trading_team.research.regime_weighted_council import (
    RegimeWeightedCouncil, build_council_decision,
)


def _snapshot(*, fresh=True, integrity="consistent"):
    now = datetime.now(timezone.utc)
    m5 = []
    price = 100.0
    # A gently rising staircase with local pullbacks creates higher highs/higher lows.
    for i in range(120):
        drift = 0.18
        pullback = 0.07 if i % 5 in (3, 4) else 0.0
        price += drift - pullback
        high = price + 0.12 + (0.03 if i % 5 == 2 else 0.0)
        low = price - 0.12 - (0.03 if i % 5 == 4 else 0.0)
        m5.append(Candle(now - timedelta(minutes=5 * (119 - i)), price - 0.04, high, low, price, 100 + i % 10))
    m15 = []
    p15 = 100.0
    for i in range(80):
        p15 += 0.22 - (0.08 if i % 5 in (3, 4) else 0.0)
        m15.append(Candle(now - timedelta(minutes=15 * (79 - i)), p15 - .04, p15 + .12, p15 - .12, p15, 150 + i % 8))
    h1 = []
    p1 = 100.0
    for i in range(65):
        p1 += 0.25 - (0.08 if i % 5 in (3, 4) else 0.0)
        h1.append(Candle(now - timedelta(hours=(64 - i)), p1 - .04, p1 + .12, p1 - .12, p1, 200 + i % 8))
    return MarketSnapshot(
        symbol="XAUUSD", price=m5[-1].close, spread=0.04,
        metadata={"quote_fresh": fresh, "timestamp_integrity_status": integrity},
        candles={"M5": m5, "M15": m15, "H1": h1},
    )


def test_council_accepts_aligned_primary_edge_and_softens_secondary_disagreement():
    result = RegimeWeightedCouncil().deliberate(
        Side.BUY, regime_score=0.82, structure_score=0.74,
        specialist_net_score=-0.30, entropy=0.40, scout_qualified=True,
    )
    assert result.take_trade is True
    assert result.side == Side.BUY
    assert result.final_score > 0.20
    assert result.size_multiplier == 0.5
    assert any("disagrees" in item for item in result.adjustments)
    assert result.to_dict()["execution_authorized"] is False


def test_council_rejects_regime_structure_disagreement_and_does_not_flip_side():
    mismatch = RegimeWeightedCouncil().deliberate(
        Side.BUY, regime_score=0.8, structure_score=-0.7,
        specialist_net_score=0.9, scout_qualified=True,
    )
    assert mismatch.take_trade is False
    assert mismatch.side == Side.FLAT
    opposite = RegimeWeightedCouncil().deliberate(
        Side.BUY, regime_score=0.8, structure_score=0.7,
        specialist_net_score=-1.0, scout_qualified=True,
    )
    assert opposite.final_score > 0
    assert opposite.side == Side.BUY
    assert opposite.size_multiplier == 0.5



def test_secondary_disagreement_cannot_veto_sufficient_primary_anchor():
    result = RegimeWeightedCouncil().deliberate(
        "BUY", regime_score=0.30, structure_score=0.30,
        specialist_net_score=-1.0, entropy=0.5, scout_qualified=True,
    )
    assert result.take_trade is True
    assert result.side == Side.BUY
    assert result.anchor_score >= 0.20
    assert result.final_score > 0  # secondary opposition cannot flip primary direction
    assert result.size_multiplier == 0.5
    assert any("bounded" in item.lower() for item in result.adjustments)

def test_uncertainty_and_volume_penalties_multiply_and_clamp():
    result = RegimeWeightedCouncil().deliberate(
        "SELL", regime_score=-0.8, structure_score=-0.8,
        specialist_net_score=-0.5, entropy=0.9, vpa_score=0.0,
        scout_qualified=True,
    )
    assert result.take_trade is True
    assert result.size_multiplier == 0.4
    result2 = RegimeWeightedCouncil().deliberate(
        "SELL", regime_score=-0.8, structure_score=-0.8,
        specialist_net_score=0.5, entropy=0.9, vpa_score=0.5,
        scout_qualified=True,
    )
    assert result2.take_trade is True
    assert result2.size_multiplier == 0.1  # .4 × .5 × .5, clamped


def test_council_requires_scout_only_when_configured():
    open_decision = RegimeWeightedCouncil().deliberate(
        "BUY", 0.8, 0.8, 0.1, scout_qualified=False, require_scout=False,
    )
    assert open_decision.take_trade is True
    gated = RegimeWeightedCouncil().deliberate(
        "BUY", 0.8, 0.8, 0.1, scout_qualified=False, require_scout=True,
    )
    assert gated.take_trade is False
    assert any("Scout baseline" in reason for reason in gated.blockers)


def test_build_council_deduplicates_correlated_families_and_ignores_non_voting():
    reports = [
        AgentReport("REGIME_ANALYST", "regime", side=Side.BUY, confidence=.8, score=.8),
        AgentReport("STRUCTURE_ANALYST", "structure", side=Side.BUY, confidence=.75, score=.7),
        AgentReport("MOMENTUM_ANALYST", "momentum", side=Side.BUY, confidence=.8, score=.6),
        AgentReport("MOMENTUM_SECONDARY", "momentum duplicate", side=Side.BUY, confidence=.9, score=.9),
        AgentReport("NEWS_ANALYST", "news", side=Side.SELL, confidence=.9, score=-.9, non_voting=True),
        AgentReport("MICROSTRUCTURE_ANALYST", "micro", side=Side.SELL, confidence=.6, score=-.3),
    ]
    snapshot = _snapshot()
    result = build_council_decision(
        reports, snapshot, scout_candidate={"qualified": True}, require_scout=True, mode="shadow",
    )
    assert result.regime_score > 0
    assert result.structure_score > 0
    assert "momentum" in result.specialist_families
    # Two momentum reports form one family rather than two council votes.
    assert len(result.specialist_families) == 2
    assert "news" not in result.specialist_families


def test_scout_scanner_qualifies_aligned_trend_snapshot_and_fails_closed_on_stale_data():
    config = ScoutConfig(min_adx=10, min_atr_ratio=0.4, min_bars=60, min_base_quality=0.1, max_spread_atr=1.0)
    scanner = ScoutScanner(config)
    snap = _snapshot()
    candidate = scanner.scan_snapshot(snap)
    assert candidate.symbol == "XAUUSD"
    assert candidate.features["m5_bars"] == 120
    assert candidate.to_dict()["execution_authorized"] is False
    stale = _snapshot(fresh=False)
    blocked = scanner.scan_snapshot(stale)
    assert blocked.qualified is False
    assert any("stale" in text.lower() for text in blocked.reasons)


def test_council_active_requires_affirmative_live_data_integrity():
    reports = [
        AgentReport("REGIME_ANALYST", "regime", side=Side.BUY, confidence=.8, score=.8),
        AgentReport("STRUCTURE_ANALYST", "structure", side=Side.BUY, confidence=.8, score=.8),
        AgentReport("MOMENTUM_ANALYST", "momentum", side=Side.BUY, confidence=.8, score=.5),
    ]
    snapshot = _snapshot(fresh=False)
    result = build_council_decision(
        reports, snapshot, scout_candidate={"qualified": True}, require_scout=True, mode="active",
    )
    assert result.take_trade is False
    assert result.scout_qualified is False
    assert any("Scout baseline" in reason for reason in result.blockers)


def test_trade_plan_council_scalar_defaults_to_legacy_full_size():
    plan = TradePlan("XAUUSD", Side.BUY, 100, 98, 104, .7)
    assert plan.size_multiplier == 1.0
    reduced = TradePlan("XAUUSD", Side.BUY, 100, 98, 104, .7, size_multiplier=.4)
    assert reduced.size_multiplier == .4


def test_agent_mesh_active_council_can_replace_secondary_floor_disagreement_but_not_risk_blockers(monkeypatch):
    from trading_team.ai_mesh import orchestrator
    from trading_team.ai_mesh.models import AgentReport, DebateReport, RiskDebate
    from trading_team.ai_mesh.debate_floor import DebateFloorResult
    from trading_team.core.models import Side

    monkeypatch.setenv("REGIME_WEIGHTED_COUNCIL_MODE", "active")
    monkeypatch.setenv("CONTINUOUS_DEBATE_ENABLED", "0")
    monkeypatch.setenv("ADAPTIVE_LEARNING_ENABLED", "0")
    monkeypatch.setenv("INSTITUTIONAL_ML_ENABLED", "0")
    monkeypatch.setenv("MIN_FINAL_CONFIDENCE", "0.58")
    monkeypatch.setenv("MIN_RESEARCH_CONFIDENCE", "0.52")
    reports = [
        AgentReport("REGIME_ANALYST", "regime", side=Side.BUY, confidence=.90, score=.82),
        AgentReport("STRUCTURE_ANALYST", "structure", side=Side.BUY, confidence=.88, score=.78),
        AgentReport("MOMENTUM_ANALYST", "momentum", side=Side.SELL, confidence=.65, score=-.3),
        AgentReport("MARKET_ANALYST", "technical", side=Side.BUY, confidence=.7, score=.4),
    ]

    def floor_for(blockers):
        return DebateFloorResult(
            rounds=3, transcript=[], final_votes={}, agreement="WAIT", agreement_confidence=.4,
            agreement_rationale="secondary specialists disagree", dissent=["secondary disagreement"],
            research=DebateReport("bull", "bear", Side.FLAT, .40, "legacy floor WAIT"),
            risk_debate=RiskDebate("aggressive", "neutral", "conservative", "neutral", .5, blockers),
        )

    monkeypatch.setattr(orchestrator, "run_analysts", lambda _snapshot, _provider: list(reports))
    holder = {"blockers": ["Research consensus is weak"]}
    monkeypatch.setattr(orchestrator, "run_debate_floor", lambda _snapshot, _reports, rounds=3: floor_for(holder["blockers"]))
    mesh = orchestrator.AgentMesh().analyze(_snapshot(), force_ai=True)
    assert mesh.regime_council["take_trade"] is True
    assert any("softened legacy consensus blocker" in reason.lower() for reason in mesh.reasons)
    assert any(
        x.get("stage") == "regime_weighted_council_authorization" and x.get("status") == "ANCHOR_ACCEPTED"
        for x in mesh.stage_trace
    )
    # A good council result still may be WAIT if later independent confidence gates
    # do not pass, but it must never bypass RiskDebate blockers.
    holder["blockers"] = ["test risk blocker"]
    blocked = orchestrator.AgentMesh().analyze(_snapshot(), force_ai=True)
    assert blocked.decision == "WAIT"
    assert any(x.get("stage") == "regime_weighted_council_authorization" and x.get("status") == "RISK_DEBATE_BLOCKED" for x in blocked.stage_trace)


def test_risk_engine_applies_council_scalar_without_bypassing_gates(monkeypatch):
    from trading_team.agents.risk import RiskAgent
    from trading_team.core.config import RiskConfig
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "0")
    snapshot = MarketSnapshot("XAUUSD", 100, .1)
    full = TradePlan("XAUUSD", Side.BUY, 100, 98, 104, .8)
    reduced = TradePlan("XAUUSD", Side.BUY, 100, 98, 104, .8, size_multiplier=.5)
    full_result = RiskAgent(RiskConfig()).evaluate(snapshot, full, 10000, 0, 0, 0)
    reduced_result = RiskAgent(RiskConfig()).evaluate(snapshot, reduced, 10000, 0, 0, 0)
    assert full_result.approved and reduced_result.approved
    assert round(reduced_result.max_loss, 6) == round(full_result.max_loss * .5, 6)
    assert round(reduced_result.quantity, 6) == round(full_result.quantity * .5, 6)
    assert next(x for x in reduced_result.checks if x["code"] == "REGIME_COUNCIL_SIZE")["passed"] is True
