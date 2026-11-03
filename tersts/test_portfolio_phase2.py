from __future__ import annotations

import math
import numpy as np
import pytest

from trading_team.research.quant_lab import portfolio
from trading_team.research.quant_lab.volatility import annualization_for_timeframe, ewma_vol
from trading_team.backtest.metrics import TradeRecord, compute_metrics


def _returns(n: int = 360) -> np.ndarray:
    rng = np.random.default_rng(20261026)
    common = rng.normal(0.00025, 0.008, n)
    a = common + rng.normal(0.00015, 0.006, n)
    b = 0.00008 + 0.65 * common + rng.normal(0.00002, 0.004, n)
    c = -0.00003 + rng.normal(0.0, 0.012, n)
    return np.column_stack((a, b, c))


def test_portfolio_allocations_are_long_only_capped_and_sum_to_one():
    result = portfolio.portfolio_from_returns(_returns(), ["EURUSD", "XAUUSD", "US500"],
                                              max_weight=0.45, capital=25000, transaction_cost_bps=5)
    for key in ("max_sharpe", "min_variance", "inverse_volatility", "equal_weight"):
        weights = result[key]["weights"]
        vals = np.asarray([weights[s] for s in result["symbols"]])
        assert np.all(vals >= -1e-8)
        assert np.all(vals <= result["max_weight_effective"] + 1e-6)
        assert float(vals.sum()) == pytest.approx(1.0, abs=1e-6)
    assert result["max_weight_effective"] == pytest.approx(0.45)


def test_portfolio_numpy_fallback_never_creates_short_or_over_cap_weights(monkeypatch):
    monkeypatch.setattr(portfolio, "HAS_SCIPY", False)
    returns = _returns(120)
    _, cov_info = portfolio._estimate_covariance(returns)
    covariance = portfolio._cov_reg(np.cov(returns, rowvar=False) * 252)
    mu = np.array([0.15, -0.04, 0.07])
    for weights in (
        portfolio.min_variance_weights(covariance, max_weight=0.4),
        portfolio.max_sharpe_weights(mu, covariance, rf=0.01, max_weight=0.4),
    ):
        assert np.all(weights >= -1e-8)
        assert np.all(weights <= 0.4 + 1e-6)
        assert float(weights.sum()) == pytest.approx(1.0, abs=1e-6)
    assert cov_info["method"] in {"ledoit_wolf", "diagonal_shrinkage_fallback"}


def test_portfolio_reports_covariance_correlation_risk_tail_and_concentration():
    result = portfolio.portfolio_from_returns(_returns(), ["A", "B", "C"], max_weight=0.6)
    estimator = result["covariance_estimator"]
    assert estimator["method"] in {"ledoit_wolf", "diagonal_shrinkage_fallback"}
    assert 0 <= estimator["shrinkage"] <= 1
    corr = np.asarray(result["correlation"]["matrix"])
    assert corr.shape == (3, 3)
    assert np.allclose(corr, corr.T, atol=1e-8)
    assert np.allclose(np.diag(corr), 1.0)
    risk = result["max_sharpe"]["risk"]
    for key in ("var_95_1d", "cvar_95_1d", "var_99_1d", "cvar_99_1d", "max_drawdown_pct",
                "effective_assets", "worst_5d_return", "estimated_one_time_rebalance_cost"):
        assert key in risk
        assert math.isfinite(float(risk[key]))
    assert 1 <= risk["effective_assets"] <= 3.00001
    contrib = np.asarray(risk["risk_contribution_pct"])
    assert contrib.shape == (3,)
    # Covariance component contributions sum to 1 except at near-zero risk.
    assert float(contrib.sum()) == pytest.approx(1.0, abs=1e-5)


def test_portfolio_cost_sensitivity_scales_one_time_cost_not_optimizer_weights():
    returns = _returns()
    names = ["EURUSD", "XAUUSD", "US500"]
    free = portfolio.portfolio_from_returns(returns, names, transaction_cost_bps=0)
    normal = portfolio.portfolio_from_returns(returns, names, transaction_cost_bps=5)
    higher = portfolio.portfolio_from_returns(returns, names, transaction_cost_bps=10)
    assert normal["max_sharpe"]["weights"] == pytest.approx(free["max_sharpe"]["weights"])
    base_cost = normal["max_sharpe"]["risk"]["estimated_one_time_rebalance_cost"]
    double_cost = higher["max_sharpe"]["risk"]["estimated_one_time_rebalance_cost"]
    assert double_cost == pytest.approx(2 * base_cost, rel=1e-6, abs=1e-8)
    assert higher["max_sharpe"]["risk"]["first_year_return_after_one_time_cost"] <= normal["max_sharpe"]["risk"]["first_year_return_after_one_time_cost"]


def test_portfolio_rejects_symbol_column_mismatch_and_invalid_returns():
    with pytest.raises(ValueError, match="columns"):
        portfolio.portfolio_from_returns(np.ones((40, 2)) * 0.001, ["A", "B", "C"])
    bad = _returns(40)
    bad[0, 0] = -1.0
    with pytest.raises(ValueError, match="greater than -100%"):
        portfolio.portfolio_from_returns(bad, ["A", "B", "C"])


def test_timeframe_annualization_and_ewma_are_timeframe_aware():
    d1, _ = annualization_for_timeframe("D1")
    m5, note = annualization_for_timeframe("M5")
    h1, _ = annualization_for_timeframe("H1")
    assert d1 == 252
    assert m5 == 252 * 288
    assert h1 == 252 * 24
    assert "session hours vary" in note
    values = np.random.default_rng(17).normal(0, 0.001, 120)
    daily = ewma_vol(values, periods_per_year=d1)
    intraday = ewma_vol(values, periods_per_year=m5)
    assert intraday["latest_vol_ann"] / daily["latest_vol_ann"] == pytest.approx(math.sqrt(m5 / d1))
    assert daily["periods_per_year"] == d1 and intraday["periods_per_year"] == m5


def test_backtest_cost_sensitivity_is_explicit_and_monotonic():
    trades = [
        TradeRecord("t0", "t1", "BUY", 100, 98, 104, 104, 1, 95, 1.0, 4,
                    gross_pnl=100, estimated_costs=5, spread_cost=2, slippage_cost=2, commission_cost=1),
        TradeRecord("t2", "t3", "SELL", 100, 102, 96, 102, 1, -25, -0.5, 3,
                    gross_pnl=-20, estimated_costs=5, spread_cost=2, slippage_cost=2, commission_cost=1),
    ]
    metrics = compute_metrics(trades, [1000, 1095, 1070], initial_equity=1000)
    rows = {row["cost_multiplier"]: row["net_pnl"] for row in metrics.cost_sensitivity}
    assert rows[0.0] == pytest.approx(80)
    assert rows[1.0] == pytest.approx(70)
    assert rows[2.0] == pytest.approx(60)
    assert metrics.cost_drag_pct_initial_equity == pytest.approx(1.0)
    assert metrics.sharpe_method == "trade_r_multiple_proxy"
    assert any("not time-bar annualized" in note for note in metrics.notes)


def test_quant_lab_service_passes_selected_timeframe_to_volatility_models():
    from trading_team.research.quant_lab.service import run_quant_lab

    closes = [100.0 * math.exp(0.0001 * i + 0.001 * math.sin(i / 4.0)) for i in range(130)]
    result = run_quant_lab(closes=closes, stages=["ewma"], timeframe="M5")
    assert result["annualization"]["periods_per_year"] == 252 * 288
    assert result["stages"]["ewma"]["periods_per_year"] == 252 * 288
    assert result["stages"]["ewma"]["latest_vol_per_bar"] > 0


def test_risk_parity_is_exposed_in_shared_portfolio_comparison_and_respects_cap():
    result = portfolio.portfolio_from_returns(_returns(), ["EURUSD", "XAUUSD", "US500"], max_weight=0.40)
    rp = result["risk_parity"]
    weights = np.asarray([rp["weights"][s] for s in result["symbols"]])
    assert np.all(weights >= -1e-8)
    assert np.all(weights <= result["max_weight_effective"] + 1e-6)
    assert float(weights.sum()) == pytest.approx(1.0, abs=1e-6)
    assert len(rp["risk"]["risk_contribution_pct"]) == 3
    assert rp["risk"]["effective_assets"] <= 3.00001


def test_portfolio_allocator_projection_preserves_sum_and_max_cap():
    from trading_team.research.portfolio_allocator import allocate

    returns = _returns(180)
    ret_map = {name: returns[:, i].tolist() for i, name in enumerate(("A", "B", "C"))}
    result = allocate(["A", "B", "C"], ret_map, method="inverse_volatility", max_weight=0.40)
    assert sum(result.weights) == pytest.approx(1.0, abs=1e-7)
    assert max(result.weights) <= 0.400001
    assert min(result.weights) >= -1e-8


def test_portfolio_allocator_raises_infeasible_minimum_weight():
    from trading_team.research.portfolio_allocator import allocate

    returns = {name: [0.001, -0.002, 0.003] * 20 for name in ("A", "B", "C")}
    with pytest.raises(ValueError, match="min_weight is infeasible"):
        allocate(["A", "B", "C"], returns, max_weight=0.8, min_weight=0.4)


def test_garch_returns_optimizer_diagnostics_for_timeframe_annualization():
    from trading_team.research.quant_lab.volatility import fit_garch11

    rng = np.random.default_rng(8102)
    # Alternating volatility blocks ensure the series isn't a constant-variance
    # fixture, while remaining deterministic for regression testing.
    low = rng.normal(0, 0.002, 180)
    high = rng.normal(0, 0.012, 180)
    rets = np.concatenate((low, high, low, high))
    out = fit_garch11(rets, periods_per_year=252 * 24)
    assert out["periods_per_year"] == 252 * 24
    assert math.isfinite(out["latest_vol_ann"])
    assert out["latest_vol_per_bar"] > 0
    assert "optimizer_converged" in out
    assert "standardized_residual_squared_lag1_corr" in out



def test_portfolio_allocator_adjusts_infeasible_max_cap_before_min_bound_check():
    from trading_team.research.portfolio_allocator import allocate

    names = ["A", "B", "C"]
    returns = {name: np.random.default_rng(i).normal(0, 0.01, 80).tolist() for i, name in enumerate(names)}
    result = allocate(names, returns, method="risk_parity", max_weight=0.20, min_weight=0.30)
    assert sum(result.weights) == pytest.approx(1.0, abs=1e-7)
    assert min(result.weights) >= 0.30 - 1e-7
    assert max(result.weights) <= (1 / 3) + 1e-7
    assert result.metadata["max_weight_requested"] == pytest.approx(0.20)
    assert result.metadata["max_weight_effective"] == pytest.approx(1 / 3)
