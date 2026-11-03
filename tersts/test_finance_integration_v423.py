from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_team.research import finance_integration as fi


def _bars(n=220):
    rng = np.random.default_rng(882)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.009, n)))
    opened = close * (1 + rng.normal(0, 0.001, n))
    high = np.maximum(opened, close) * 1.003
    low = np.minimum(opened, close) * 0.997
    times = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC").astype("int64") // 10**9
    return [{"time": int(t), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}
            for t, o, h, l, c, v in zip(times, opened, high, low, close, rng.integers(10, 500, n))]


def test_finance_indicators_accept_realistic_mt5_rows_and_label_data_source():
    result = fi.analyze_indicators(_bars(), symbol="EURUSD", timeframe="D1")
    assert result["symbol"] == "EURUSD"
    assert result["data_source"] == "MT5 verified OHLCV"
    assert result["bars"] == 220
    assert {x["label"] for x in result["metrics"]} >= {"RSI 14", "ATR 14", "ADX 14", "Supertrend"}


def test_finance_context_handles_generator_bars_without_consuming_them_twice():
    result = fi.shared_technical_context({"D1": iter(_bars())}, symbol="EURUSD")
    assert result["available"] is True
    assert "D1" in result["timeframes"]


def test_dcf_scenarios_are_assumption_driven_and_have_sensitivity_grid():
    result = fi.discounted_cash_flow_scenarios(
        cash_flows=[100, 110, 120, 130, 140], discount_rate=0.10, terminal_growth=0.025,
        cash=50, debt=20, shares=10,
    )
    assert result["ok"] is True
    assert result["enterprise_value"] > 0
    assert result["equity_value"] == pytest.approx(result["enterprise_value"] + 30)
    assert result["value_per_share"] == pytest.approx(result["equity_value"] / 10)
    assert len(result["sensitivity"]) >= 5
    assert "user-entered projections" in result["warning"]


def test_dcf_rejects_terminal_growth_at_or_above_discount_rate():
    with pytest.raises(ValueError, match="Terminal growth"):
        fi.discounted_cash_flow_scenarios(cash_flows=[100, 120], discount_rate=0.05, terminal_growth=0.05)


def test_company_snapshot_uses_on_demand_provider_and_json_safe_fields(monkeypatch):
    import finance.data.providers as providers

    class FakeYahoo:
        def __init__(self, timeout):
            assert 2 <= timeout <= 15
        def company(self, ticker):
            assert ticker == "AAPL"
            return pd.Series({"name": "Example Corp", "price": np.float64(123.4), "market_cap": np.float64(1e9), "pe": np.nan})

    monkeypatch.setattr(providers, "YahooFinance", FakeYahoo)
    result = fi.fetch_company_snapshot("aapl")
    assert result["provider"] == "Yahoo Finance"
    assert result["fields"]["price"] == pytest.approx(123.4)
    assert result["fields"]["pe"] is None
    assert result["data_limits"]


def test_company_statements_are_bounded_and_retain_periods(monkeypatch):
    import finance.data.providers as providers

    class FakeYahoo:
        def __init__(self, timeout): pass
        def statements(self, ticker, kind, frequency):
            assert ticker == "MSFT" and kind == "income" and frequency == "annual"
            data = pd.DataFrame({"Revenue": range(7), "Net Income": range(10, 17)},
                                index=pd.date_range("2019-12-31", periods=7, freq="YE"))
            data.attrs["currency"] = "USD"
            data.attrs["restated"] = True
            return data

    monkeypatch.setattr(providers, "YahooFinance", FakeYahoo)
    result = fi.fetch_company_statement("MSFT", kind="income", frequency="annual")
    assert len(result["periods"]) == 5
    assert len(result["metrics"]) == 2
    assert result["currency"] == "USD"


def test_headline_sentiment_is_explicitly_headline_only(monkeypatch):
    import finance.data.providers as providers
    import importlib
    sent = importlib.import_module("finance.analytics.sentiment")

    class FakeYahoo:
        def __init__(self, timeout): pass
        def news(self, ticker):
            return pd.DataFrame([{"title": "Company reports strong growth", "published": "2026-01-01", "url": "https://example.com/a"}])

    monkeypatch.setattr(providers, "YahooFinance", FakeYahoo)
    monkeypatch.setattr(sent, "sentiment", lambda texts: pd.DataFrame([{"compound": 0.7, "pos": 0.6, "neu": 0.4, "neg": 0.0}]))
    result = fi.fetch_company_headline_sentiment("AAPL")
    assert result["count"] == 1
    assert result["headlines"][0]["label"] == "POSITIVE_HEADLINE"
    assert "headlines only" in result["warning"]


def test_public_finance_routes_are_separate_from_mt5_only_routes():
    repo = Path(__file__).resolve().parents[1]
    app = (repo / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    html = (repo / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (repo / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    for route in ("/api/finance/company-snapshot", "/api/finance/company-statements", "/api/finance/company-news", "/api/finance/dcf"):
        assert route in app
    assert "data-finance-tab=\"company\"" in html
    for fn in ("runFinanceCompanySnapshot", "runFinanceCompanyStatements", "runFinanceCompanyNews", "runFinanceDCF"):
        assert f"window.{fn}={fn}" in js
    assert "finance_public_routes" in app


def _post_handler(path, payload):
    import io
    import json
    from trading_team.web.app import Handler

    handler = Handler.__new__(Handler)
    handler.path = path
    raw = json.dumps(payload).encode("utf-8")
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    captured = {}
    handler.send_json = lambda obj, code=200: captured.update(payload=obj, status=code)
    handler.do_POST()
    return captured


def test_dcf_http_endpoint_returns_result_without_mt5_connection():
    result = _post_handler("/api/finance/dcf", {
        "cash_flows": [100, 110, 120, 130], "discount_rate": 0.10,
        "terminal_growth": 0.02, "cash": 10, "debt": 5, "shares": 10,
    })
    assert result["status"] == 200
    assert result["payload"]["ok"] is True
    assert result["payload"]["sensitivity"]


def test_public_company_http_route_is_explicit_and_json_safe(monkeypatch):
    from trading_team.research import finance_integration as module
    monkeypatch.setattr(module, "fetch_company_snapshot", lambda ticker: {
        "ok": True, "ticker": ticker.upper(), "provider": "fake", "fields": {"price": 12.5}
    })
    result = _post_handler("/api/finance/company-snapshot", {"ticker": "demo"})
    assert result["status"] == 200
    assert result["payload"]["ticker"] == "DEMO"
    assert result["payload"]["fields"]["price"] == 12.5
