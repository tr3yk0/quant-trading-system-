from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from trading_team.core.models import Candle, MarketSnapshot
from trading_team.research.quant_context import (
    build_quant_context,
    clear_quant_context_cache,
    enrich_snapshot_quant,
)


def _candles(count: int = 180, minutes: int = 5) -> list[Candle]:
    now = datetime.now(timezone.utc)
    result = []
    prev = 1.10
    for i in range(count):
        # Deterministic trend with oscillation: sufficient for regression tests,
        # without claiming this synthetic series represents a market.
        close = 1.10 * math.exp(0.00012 * i + 0.00045 * math.sin(i / 7.0))
        open_ = prev
        result.append(Candle(
            timestamp=now - timedelta(minutes=minutes * (count - 1 - i)),
            open=open_, high=max(open_, close) * 1.0004,
            low=min(open_, close) * 0.9996, close=close, volume=100 + i,
        ))
        prev = close
    return result


def _snapshot(**metadata) -> MarketSnapshot:
    bars = {
        "M5": _candles(180, 5),
        "M15": _candles(180, 15),
        "H1": _candles(180, 60),
    }
    return MarketSnapshot(
        symbol="TEST.EURUSD", price=bars["M5"][-1].close, spread=0.00008,
        metadata=dict(metadata), candles=bars,
    )


def test_shared_quant_context_contains_causal_multitimeframe_and_risk_fields():
    clear_quant_context_cache()
    snapshot = _snapshot(mt5_connected=True, realtime=True, quote_fresh=True, demo=False)
    context = build_quant_context(snapshot)

    assert context["symbol"] == "TEST.EURUSD"
    assert set(("M5", "M15", "H1")).issubset(context["timeframes"])
    assert context["consensus"]["side"] in {"BUY", "SELL", "FLAT"}
    assert -1.0 <= context["consensus"]["score"] <= 1.0
    assert 0.0 <= context["consensus"]["agreement"] <= 1.0
    assert context["regime"]["label"] in {
        "HIGH_VOL_SHOCK", "LOW_VOL_COMPRESSION", "TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION"
    }
    assert "historical_var_95_per_bar" in context["risk_context"]
    assert context["market_quality"]["source_is_live"] is True
    # Shared price-only features must not fabricate an option chain or market IV.
    assert "implied_volatility" not in context
    assert "option_chain" not in context


def test_quant_context_cache_key_tracks_market_data_provenance_changes():
    clear_quant_context_cache()
    snapshot = _snapshot(mt5_connected=True, realtime=True, quote_fresh=True)
    live_context = build_quant_context(snapshot)
    snapshot.metadata["mt5_connected"] = False
    offline_context = build_quant_context(snapshot)

    assert live_context["fingerprint"] != offline_context["fingerprint"]
    assert live_context["market_quality"]["source_is_live"] is True
    assert offline_context["market_quality"]["source_is_live"] is False


def test_enrichment_is_shared_through_market_context_and_compact_prompt():
    from trading_team.ai_mesh.context import build_market_context, compact_prompt_context

    snapshot = _snapshot(mt5_connected=True, realtime=True, quote_fresh=True)
    enriched = enrich_snapshot_quant(snapshot)
    assert enriched.metadata["quant_context"]["symbol"] == snapshot.symbol
    context = build_market_context(snapshot)
    assert context["quantitative"]["fingerprint"] == snapshot.metadata["quant_context"]["fingerprint"]
    prompt = compact_prompt_context(context)
    assert "Shared quant context" in prompt
    assert "Execution/risk context" in prompt


def test_portfolio_quant_warnings_reduce_proposed_size_without_becoming_hard_vetoes():
    from trading_team.agents.portfolio import PortfolioAgent

    snapshot = MarketSnapshot(
        symbol="TEST.EURUSD", price=1.1, spread=0.0008,
        metadata={
            "equity": 10_000.0,
            "quant_context": {
                "regime": {"label": "HIGH_VOL_SHOCK"},
                "risk_context": {"historical_var_95_per_bar": 0.001, "historical_cvar_95_per_bar": 0.002},
                "execution_cost": {"state": "poor"},
                "data_limits": [],
            },
        },
    )
    signal = PortfolioAgent().analyze(snapshot)
    assert signal.score < 1.0
    assert signal.score == 0.7225
    assert any("volatility shock" in item.lower() for item in signal.evidence)
    assert any("spread" in item.lower() for item in signal.evidence)
    assert not any("volatility shock" in item.lower() or "spread" in item.lower() for item in signal.invalidations)


def test_quantlab_pricing_exposes_quantlib_as_shared_comparison_when_missing(monkeypatch):
    from trading_team.research.quant_lab import service
    from trading_team.research.quant_lab import quantlib_adapter

    def quick_mc(*_args, **_kwargs):
        return {"price": 2.0, "std_error": 0.01, "terminal": [100.0]}

    monkeypatch.setattr(service, "mc_price", quick_mc)
    monkeypatch.setattr(service, "pde_price", lambda *_args, **_kwargs: 2.1)
    monkeypatch.setattr(service, "bs_price", lambda *_args, **_kwargs: 2.05)
    monkeypatch.setattr(quantlib_adapter, "quantlib_status", lambda: {
        "available": False, "error": "QuantLib Python bindings are not installed"
    })
    closes = [100.0 + 0.02 * i + 0.1 * math.sin(i) for i in range(80)]
    result = service.run_quant_lab(
        closes=closes,
        option={"spot": 101.0, "strike": 100.0, "T": 30 / 365, "sigma": 0.2},
        stages=["pricing"],
    )
    pricing = result["stages"]["pricing"]
    assert pricing["quantlib"]["available"] is False
    assert "quantlib" in pricing["quantlib"]["reason"].lower()
    assert "bs_closed_form" in pricing["engines"]
    assert "market_implied" not in pricing["engines"]["bs_closed_form"]


def test_ai_mesh_quant_and_volatility_analysts_consume_shared_context():
    from trading_team.ai_mesh.analysts import _deterministic

    snapshot = _snapshot(mt5_connected=True, realtime=True, quote_fresh=True)
    enrich_snapshot_quant(snapshot)
    quant_report = _deterministic("QUANT_ANALYST", snapshot)
    vol_report = _deterministic("VOLATILITY_ANALYST", snapshot)
    market_report = _deterministic("MARKET_ANALYST", snapshot)

    assert any("Method=" in item for item in quant_report.evidence)
    assert any("vol regime" in item.lower() or "historical per-bar" in item.lower() for item in vol_report.evidence)
    assert any("SHARED_QUANT|" in item for item in market_report.evidence)


def test_quantlab_prices_american_options_with_quantlib_binomial_when_available(monkeypatch):
    from trading_team.research.quant_lab import service, quantlib_adapter

    def quick_mc(*_args, **_kwargs):
        return {"price": 2.0, "std_error": 0.01, "terminal": [100.0]}

    captured = {}

    def fake_price(payload):
        captured.update(payload)
        return {
            "price": 2.5, "engine": "BinomialVanillaEngine(CRR, steps=64)",
            "greeks": {"delta": 0.5, "gamma": 0.01, "vega": 0.2, "theta": -0.1, "rho": 0.05},
            "implied_volatility": None, "library_version": "1.44",
        }

    monkeypatch.setattr(service, "mc_price", quick_mc)
    monkeypatch.setattr(service, "pde_price", lambda *_args, **_kwargs: 2.1)
    monkeypatch.setattr(service, "bs_price", lambda *_args, **_kwargs: 2.05)
    monkeypatch.setattr(quantlib_adapter, "quantlib_status", lambda: {"available": True, "quantlib_version": "1.44"})
    monkeypatch.setattr(quantlib_adapter, "price_option", fake_price)

    closes = [100.0 + 0.02 * i + 0.1 * math.sin(i) for i in range(80)]
    result = service.run_quant_lab(
        closes=closes,
        option={"spot": 101.0, "strike": 100.0, "T": 30 / 365, "sigma": 0.2,
                "exercise": "american", "is_call": False, "steps": 64},
        stages=["pricing"],
    )
    pricing = result["stages"]["pricing"]
    assert captured["exercise"] == "american"
    assert captured["engine"] == "binomial"
    assert captured["option_type"] == "put"
    assert pricing["engines"]["quantlib_option"]["greeks"]["delta"] == 0.5
    assert pricing["engines"]["quantlib_option"]["market_implied"] is False
