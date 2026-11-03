from __future__ import annotations

from datetime import datetime, timezone
import math

import pytest

from trading_team.research.portfolio_analytics import (
    analyze_account_performance,
    classify_account_adjustments,
    modified_dietz_return,
)


def test_modified_dietz_removes_external_deposit_effect_with_time_weighting():
    out = modified_dietz_return(
        1000, 1200, 1000, 2000,
        [{"time": 1500, "amount": 100, "classification": "balance_flow_candidate"}],
    )
    assert out["status"] == "ok"
    # Investment made 100 organic profit. Dietz denominator is 1,050 due to mid-period flow.
    assert out["return"] == pytest.approx(100 / 1050)
    assert out["cashflow_amount"] == 100


def test_unknown_credit_correction_and_bonus_are_separated_for_review():
    out = classify_account_adjustments([
        {"ticket": 1, "time": 100, "type": 2, "profit": 500, "comment": "Deposit"},
        {"ticket": 2, "time": 200, "type": 3, "profit": 50, "comment": "Credit"},
        {"ticket": 3, "time": 300, "type": 5, "profit": 15, "comment": "Correction"},
        {"ticket": 4, "time": 400, "type": 6, "profit": 25, "comment": "Bonus"},
        {"ticket": 5, "time": 500, "type": 7, "profit": -2, "comment": "Commission"},
    ])
    assert len(out["external_cashflow_candidates"]) == 1
    assert out["external_cashflow_candidates"][0]["amount"] == 500
    assert {x["type_name"] for x in out["adjustments_requiring_review"]} == {"CREDIT", "CORRECTION", "BONUS"}
    assert len(out["internal_income_cost_events"]) == 1


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def test_account_analytics_reports_dietz_xirr_benchmark_and_proxy_regression():
    # 45 daily account marks, one known deposit, and noisy but deterministic market data.
    base = 1_790_000_000  # a fixed UTC date/time for repeatability
    snapshots = []
    for i in range(45):
        ts = base + i * 86400 + 23 * 3600
        equity = 1000 * (1.001 ** i) + (100 if i >= 20 else 0)
        snapshots.append({"timestamp_epoch": ts, "time_utc": _iso(ts), "equity": equity,
                          "balance": equity, "currency": "USD"})
    flow_ts = base + 20 * 86400 + 12 * 3600
    deals = [{"ticket": 1, "time": flow_ts, "time_iso": _iso(flow_ts), "type": 2,
              "profit": 100, "comment": "Deposit", "symbol": ""}]
    rates_bench, rates_factor = [], []
    bench = 100.0
    factor = 2000.0
    for i in range(45):
        ts = base + i * 86400
        bench *= 1 + (0.0003 + 0.003 * math.sin(i / 3.0))
        factor *= 1 + (0.0001 + 0.002 * math.cos(i / 4.0))
        rates_bench.append({"time": ts, "close": bench})
        rates_factor.append({"time": ts, "close": factor})
    result = analyze_account_performance(snapshots, deals, days=60, benchmark_symbol="US500",
                                          market_rates={"US500": rates_bench, "XAUUSD": rates_factor})
    perf = result["cashflow_adjusted_performance"]
    assert result["account_history"]["snapshot_count"] == 45
    assert perf["status"] == "estimated_modified_dietz_linked"
    assert perf["external_cashflow_count"] == 1
    assert perf["time_weighted_return_pct_estimate"] is not None
    assert perf["money_weighted_annualized_return_pct_estimate"] is not None
    assert result["benchmark"]["status"] == "ok"
    assert result["benchmark"]["observations"] >= 30
    assert result["benchmark"]["beta"] is not None
    assert len(result["benchmark"]["comparison_curve"]) >= 30
    assert result["factor_model"]["status"] in {"ok", "collinear_factors"}
    assert "US500" in result["factor_model"]["betas"]
    assert "XAUUSD" in result["factor_model"]["betas"]
    assert len(result["benchmark"]["comparison_curve"]) >= 30


def test_account_analytics_withholds_cashflow_adjusted_metrics_on_unclassified_adjustments():
    base = 1_790_000_000
    snaps = []
    for i, eq in enumerate([1000, 1010, 1020, 1030]):
        ts = base + i * 86400
        snaps.append({"timestamp_epoch": ts, "equity": eq})
    deals = [{"ticket": 1, "time": base + 2 * 86400 - 10, "type": 3,
              "profit": 50, "comment": "Broker credit"}]
    result = analyze_account_performance(snaps, deals, days=10)
    perf = result["cashflow_adjusted_performance"]
    assert perf["status"] == "adjustments_require_review"
    assert perf["time_weighted_return_pct_estimate"] is None
    assert perf["money_weighted_annualized_return_pct_estimate"] is None
    assert perf["adjustments_requiring_review"] == 1


def test_analytics_returns_insufficient_observations_for_short_benchmark_history():
    base = 1_790_000_000
    snapshots, rates = [], []
    for i in range(9):
        snapshots.append({"timestamp_epoch": base + i * 86400 + 23 * 3600, "equity": 1000 + i})
        rates.append({"time": base + i * 86400, "close": 100 + i * 0.3 + (i % 2)})
    result = analyze_account_performance(snapshots, [], days=20, benchmark_symbol="US500", market_rates={"US500": rates})
    assert result["benchmark"]["status"] == "insufficient_observations"
    assert result["benchmark"]["required_observations"] == 20


def test_missing_selected_benchmark_still_calculates_available_proxy_factor_model():
    base = 1_790_000_000
    snapshots, xau, eur = [], [], []
    xau_close, eur_close = 1900.0, 1.08
    for i in range(46):
        ts = base + i * 86400
        snapshots.append({"timestamp_epoch": ts + 23 * 3600, "equity": 2000 * (1.0005 ** i)})
        xau_close *= 1 + 0.0002 + 0.0020 * math.sin(i / 3.0)
        eur_close *= 1 + 0.0001 + 0.0012 * math.cos(i / 4.0)
        xau.append({"time": ts, "close": xau_close})
        eur.append({"time": ts, "close": eur_close})
    result = analyze_account_performance(
        snapshots, [], days=60, benchmark_symbol="BROKER_INDEX_MISSING",
        market_rates={"XAUUSD": xau, "EURUSD": eur},
    )
    assert result["benchmark"]["status"] == "market_data_unavailable"
    model = result["factor_model"]
    assert model["status"] in {"ok", "collinear_factors"}
    assert model["benchmark_symbol_unavailable"] == "BROKER_INDEX_MISSING"
    assert set(model["betas"]) == {"XAUUSD", "EURUSD"}


def test_single_bar_benchmark_is_reported_as_insufficient_history_not_not_requested():
    base = 1_790_000_000
    snapshots = [
        {"timestamp_epoch": base + i * 86400 + 23 * 3600, "equity": 1000 + i * 5}
        for i in range(4)
    ]
    result = analyze_account_performance(
        snapshots, [], days=10, benchmark_symbol="US500",
        market_rates={"US500": [{"time": base, "close": 5000.0}]},
    )
    assert result["benchmark"]["status"] == "insufficient_market_history"
    assert "two" in result["benchmark"]["error"]


def test_requested_benchmark_without_market_rows_is_not_left_as_not_requested():
    base = 1_790_000_000
    snapshots = [
        {"timestamp_epoch": base + i * 86400 + 23 * 3600, "equity": 1000 + i * 3}
        for i in range(4)
    ]
    result = analyze_account_performance(snapshots, [], days=10, benchmark_symbol="BROKER_BENCH", market_rates={})
    assert result["benchmark"]["status"] == "market_data_unavailable"
    assert result["benchmark"]["symbol"] == "BROKER_BENCH"
