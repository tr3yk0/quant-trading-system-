from __future__ import annotations

import json
from pathlib import Path

from trading_team.research.experiment_ledger import ExperimentLedger, fingerprint_candles, validation_tier
from trading_team.research.strategy_registry import StrategyRegistry


def _candles(n=40):
    return [
        {"time": 1_700_000_000 + i * 300, "open": 100 + i * 0.1, "high": 101 + i * 0.1,
         "low": 99 + i * 0.1, "close": 100.5 + i * 0.1, "volume": i + 1}
        for i in range(n)
    ]


def test_fingerprint_is_deterministic_and_sensitive_to_price_or_order():
    rows = _candles()
    first = fingerprint_candles(rows, symbol="xauusd", timeframe="m5")
    second = fingerprint_candles(rows, symbol="XAUUSD", timeframe="M5")
    assert first["sha256"] == second["sha256"]
    assert first["bars"] == 40
    changed = [dict(row) for row in rows]
    changed[10]["close"] += 0.1
    assert fingerprint_candles(changed, symbol="XAUUSD", timeframe="M5")["sha256"] != first["sha256"]
    assert fingerprint_candles(list(reversed(rows)), symbol="XAUUSD", timeframe="M5")["sha256"] != first["sha256"]


def test_ledger_records_bounded_compact_reproducibility_metadata(tmp_path):
    ledger = ExperimentLedger(tmp_path / "experiments.db")
    config = {"strategy_id": "breakout", "risk_fraction": 0.005,
              "csv": r"C:\Users\Example\Downloads\bars.csv", "commission_bps": 1.5}
    result = {"net_pnl": 125.5, "gross_pnl": 140.0, "estimated_costs": 14.5,
              "equity_curve": list(range(2000)), "trade_log": [{"secretly_large": True}] * 2000,
              "cost_sensitivity": [{"cost_multiplier": 1, "net_pnl": 125.5}],
              "data_source": r"CSV:C:\Users\Example\Downloads\bars.csv"}
    record = ledger.record(kind="strategy_backtest", symbol="XAUUSD", timeframe="M5", strategy_id="breakout",
                           data_source=result["data_source"], candles=_candles(), config=config, result=result,
                           mode="single", execution_model="ohlc")
    loaded = ledger.get(record["experiment_id"])
    assert loaded is not None
    assert loaded["research_only"] is True
    assert loaded["validation_tier"] == "SINGLE_BACKTEST_UNVALIDATED"
    assert loaded["data_fingerprint"] == record["data_fingerprint"]
    assert loaded["config"]["csv_name"] == "bars.csv"
    assert "C:\\Users" not in json.dumps(loaded)
    assert "equity_curve" not in loaded["metrics"]
    assert "trade_log" not in loaded["metrics"]
    assert loaded["metrics"]["automatic_promotion"] is False
    assert loaded["data_source"] == "user-supplied CSV/history file"
    assert ledger.list(limit=500)[0]["experiment_id"] == record["experiment_id"]


def test_walkforward_validation_tier_requires_real_folds():
    assert validation_tier("strategy_optimization", "walk_forward", {"folds": [{}, {}]}) == "WALK_FORWARD_OOS_REVIEW_REQUIRED"
    assert validation_tier("strategy_optimization", "grid", {"trials": [{}, {}], "n_trials": 2}) == "PARAMETER_SEARCH_ONLY_OOS_REQUIRED"
    assert validation_tier("strategy_backtest", "single", {}) == "SINGLE_BACKTEST_UNVALIDATED"


def test_strategy_registry_attaches_evidence_without_changing_status(tmp_path):
    registry = StrategyRegistry(db_path=tmp_path / "strategy.db")
    sid = registry.register("Breakout", version="1.0.0", status="PAPER", evidence={"strategy_id": "breakout"})
    ok = registry.attach_research_experiment("breakout", {"experiment_id": "exp_test", "research_only": True})
    row = next(x for x in registry.list(20) if x["strategy_id"] == sid)
    assert ok is True
    assert row["status"] == "PAPER"
    assert row["evidence"]["latest_research_experiment"]["experiment_id"] == "exp_test"
    assert row["evidence"]["automatic_promotion"] is False
    registry.db.close()


def test_experiment_list_and_detail_routes_are_read_only_and_bounded(tmp_path, monkeypatch):
    import trading_team.research.experiment_ledger as ledger_module
    from trading_team.web.app import Handler

    ledger = ExperimentLedger(tmp_path / "route.db")
    experiment = ledger.record(kind="strategy_backtest", symbol="EURUSD", timeframe="M5", strategy_id="trend_following",
                               data_source="MT5", candles=_candles(), config={}, result={"net_pnl": 3},
                               mode="single", execution_model="ohlc")
    monkeypatch.setattr(ledger_module, "ExperimentLedger", lambda: ledger)

    def call(path):
        handler = Handler.__new__(Handler)
        handler.path = path
        captured = {}
        handler.send_json = lambda obj, code=200: captured.update(payload=obj, status=code)
        handler.do_GET()
        return captured

    listed = call("/api/research/experiments?limit=999999")
    assert listed["status"] == 200
    assert listed["payload"]["count"] == 1
    detail = call("/api/research/experiments/" + experiment["experiment_id"])
    assert detail["status"] == 200
    assert detail["payload"]["item"]["research_only"] is True
    missing = call("/api/research/experiments/exp_missing")
    assert missing["status"] == 404


def test_ui_normalizes_walkforward_mode_and_exposes_ledger():
    repo = Path(__file__).resolve().parents[1]
    js = (repo / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    html = (repo / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    app = (repo / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    assert "mode:body.mode||'grid'" in js
    assert "refreshResearchExperiments" in js
    assert 'id="researchExperimentBody"' in html
    assert '"/api/research/experiments"' in app
    assert 'replace("_", "")' in app


def test_backtest_route_persists_successful_run_and_exposes_fingerprint(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import io
    import trading_team.backtest.io as bt_io
    import trading_team.research.experiment_ledger as ledger_module
    import trading_team.web.app as web_app
    from trading_team.core.models import Candle

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(520):
        close = 2000.0 + i * 0.012 + 2.5 * __import__("math").sin(i / 8.0)
        opened = close - 0.12 * __import__("math").cos(i / 4.0)
        candles.append(Candle(base + timedelta(minutes=5 * i), opened,
                              max(opened, close) + 0.25, min(opened, close) - 0.25, close, 100 + i % 7))

    monkeypatch.setattr(bt_io, "resolve_backtest_candles", lambda **kwargs: (candles, "MOCKED VERIFIED TEST BARS"))
    ledger = ExperimentLedger(tmp_path / "http-run.db")
    monkeypatch.setattr(ledger_module, "ExperimentLedger", lambda: ledger)
    monkeypatch.setattr(web_app.RUNTIME.strategy_registry, "attach_research_experiment", lambda *_args, **_kwargs: True)

    import json
    handler = web_app.Handler.__new__(web_app.Handler)
    payload = {"symbol": "XAUUSD", "timeframe": "M5", "bars": 520, "strategy_id": "breakout",
               "equity": 10000, "risk_fraction": 0.005, "execution_model": "ohlc",
               "commission_bps": 1.0, "include_trades": True}
    raw = json.dumps(payload).encode()
    handler.path = "/api/backtest/strategy"
    handler.command = "POST"
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    captured = {}
    handler.send_json = lambda obj, code=200: captured.update(payload=obj, status=code)
    handler.do_POST()

    assert captured["status"] == 200, captured.get("payload")
    assert captured["payload"]["experiment"]["research_only"] is True
    assert captured["payload"]["experiment"]["data_fingerprint"]
    saved = ledger.get(captured["payload"]["experiment"]["experiment_id"])
    assert saved is not None
    assert saved["strategy_id"] == "breakout"
    assert saved["bars"] == 520
    assert saved["metrics"]["automatic_promotion"] is False


def test_fingerprint_includes_historical_execution_quotes():
    rows = _candles(40)
    from datetime import datetime, timezone, timedelta
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes = [{"timestamp": start + timedelta(seconds=i), "bid": 100.0 + i * 0.01,
               "ask": 100.02 + i * 0.01, "last": 0.0, "volume": 1.0} for i in range(10)]
    base = fingerprint_candles(rows, symbol="EURUSD", timeframe="M5")
    tick_hash = fingerprint_candles(rows, symbol="EURUSD", timeframe="M5", execution_quotes=quotes)
    changed_quotes = [dict(q) for q in quotes]
    changed_quotes[2]["ask"] += 0.01
    changed_hash = fingerprint_candles(rows, symbol="EURUSD", timeframe="M5", execution_quotes=changed_quotes)
    assert tick_hash["execution_quotes"] == 10
    assert tick_hash["sha256"] != base["sha256"]
    assert tick_hash["sha256"] != changed_hash["sha256"]


def test_walkforward_api_mode_does_not_fall_back_to_grid_search(tmp_path, monkeypatch):
    import io
    import json
    import trading_team.backtest.io as bt_io
    import trading_team.backtest.optimizer as optimizer
    import trading_team.research.experiment_ledger as ledger_module
    import trading_team.web.app as web_app

    candles = _candles(520)
    monkeypatch.setattr(bt_io, "resolve_backtest_candles", lambda **kwargs: (candles, "MOCKED VERIFIED TEST BARS"))
    called = {"walkforward": False, "grid": False}

    class FakeResult:
        def to_dict(self):
            return {"folds": [{"fold": 1, "is_score": 0.2, "oos_score": 0.1},
                              {"fold": 2, "is_score": 0.3, "oos_score": 0.08}],
                    "avg_is_score": 0.25, "avg_oos_score": 0.09, "robust": False,
                    "notes": ["review OOS results"]}

    def fake_walkforward(*args, **kwargs):
        called["walkforward"] = True
        return FakeResult()

    def fake_grid(*args, **kwargs):
        called["grid"] = True
        raise AssertionError("Walk-forward request was incorrectly sent to grid search")

    monkeypatch.setattr(optimizer, "walk_forward", fake_walkforward)
    monkeypatch.setattr(optimizer, "grid_search", fake_grid)
    ledger = ExperimentLedger(tmp_path / "walkforward.db")
    monkeypatch.setattr(ledger_module, "ExperimentLedger", lambda: ledger)
    monkeypatch.setattr(web_app.RUNTIME.strategy_registry, "attach_research_experiment", lambda *_args, **_kwargs: True)

    body = {"symbol": "EURUSD", "timeframe": "M5", "bars": 520, "strategy_id": "breakout",
            "equity": 10000, "risk_fraction": 0.005, "execution_model": "ohlc",
            "mode": "walk_forward", "folds": 2, "is_ratio": 0.7}
    handler = web_app.Handler.__new__(web_app.Handler)
    raw = json.dumps(body).encode()
    handler.path = "/api/backtest/optimize"
    handler.command = "POST"
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    captured = {}
    handler.send_json = lambda obj, code=200: captured.update(payload=obj, status=code)
    handler.do_POST()

    assert captured["status"] == 200, captured.get("payload")
    assert called == {"walkforward": True, "grid": False}
    assert captured["payload"]["experiment"]["validation_tier"] == "WALK_FORWARD_OOS_REVIEW_REQUIRED"
