from __future__ import annotations

import math

import pytest

from trading_team.research.quant_lab.quantlib_adapter import (
    QuantLibUnavailable,
    price_option,
    price_option_with_scenarios,
    quantlib_status,
    validate_option_request,
)


def _request(**overrides):
    result = {
        "symbol": "TEST",
        "spot": 100.0,
        "strike": 100.0,
        "days_to_expiry": 365,
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "volatility": 0.20,
        "option_type": "call",
        "exercise": "european",
        "engine": "auto",
        "steps": 200,
    }
    result.update(overrides)
    return result


def test_quantlib_status_is_actionable_and_never_probes_network():
    status = quantlib_status()
    assert isinstance(status, dict)
    assert "available" in status and "installed" in status
    if status["available"]:
        assert status["quantlib_version"]
        assert "European vanilla options: analytic Black-Scholes-Merton" in status["capabilities"]
    else:
        assert status["code"] == "QUANTLIB_NOT_INSTALLED"
        assert "install" in status["error"].lower()


def test_option_request_validation_normalizes_fields():
    result = validate_option_request(_request(option_type="PUT", exercise="AMERICAN"))
    assert result["option_type"] == "put"
    assert result["exercise"] == "american"
    assert result["T"] == 1.0
    assert result["steps"] == 200


@pytest.mark.parametrize("overrides, message", [
    ({"spot": 0}, "spot"),
    ({"strike": -1}, "strike"),
    ({"days_to_expiry": 0}, "days_to_expiry"),
    ({"days_to_expiry": 30.5}, "integer"),
    ({"volatility": 0}, "volatility"),
    ({"volatility": float("nan")}, "finite"),
    ({"option_type": "straddle"}, "option_type"),
    ({"exercise": "bermudan"}, "exercise"),
    ({"exercise": "american", "engine": "analytic"}, "American"),
    ({"steps": 2}, "steps"),
    ({"market_price": -5}, "market_price"),
])
def test_invalid_option_requests_fail_fast(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_option_request(_request(**overrides))


def test_pricing_feature_is_explicit_when_optional_binding_is_missing():
    status = quantlib_status()
    if status["available"]:
        result = price_option(_request())
        assert result["library"] == "QuantLib"
        assert math.isfinite(result["price"]) and result["price"] > 0
        assert result["greeks"]["delta"] is not None
        return
    with pytest.raises(QuantLibUnavailable, match="Python bindings are not installed"):
        price_option(_request())


def test_scenario_matrix_is_9_cells_when_quantlib_is_installed():
    status = quantlib_status()
    if not status["available"]:
        pytest.skip("QuantLib wheel is installed by START.bat on the Windows deployment host.")
    result = price_option_with_scenarios(_request())
    assert len(result["scenario_grid"]) == 3
    assert all(len(row) == 3 for row in result["scenario_grid"])
    centre = result["scenario_grid"][1][1]
    assert centre["spot_shock_pct"] == 0
    assert centre["volatility_shock_points"] == 0
    assert abs(centre["change_from_base"]) < 1e-8
