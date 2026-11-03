from __future__ import annotations
import pytest
from trading_team.research.portfolio_suite import (
    summarize_live_book, summarize_closed_deals, build_stress_scenarios, build_rebalance_proposal,
)


def test_live_book_flags_unprotected_positions_and_preserves_account_currency():
    out = summarize_live_book(
        {"balance": 1000, "equity": 1050, "profit": 50, "margin": 100, "margin_free": 950,
         "margin_level": 1050, "currency": "USD", "leverage": 100},
        [
            {"ticket": 1, "symbol": "EURUSD", "side": "BUY", "volume": .1, "profit": 40,
             "swap": -1, "commission": -1, "sl": 1.05, "stop_risk_cash": 12},
            {"ticket": 2, "symbol": "XAUUSD", "side": "SELL", "volume": .2, "profit": 10,
             "swap": 0, "commission": 0, "sl": 0, "stop_risk_cash": None},
        ], [{"ticket": 9, "symbol": "GBPUSD"}], generated_at="now")
    assert out["account"]["currency"] == "USD"
    assert out["summary"]["open_positions"] == 2
    assert out["summary"]["pending_orders"] == 1
    assert out["summary"]["positions_without_stop"] == 1
    assert out["summary"]["stop_risk_coverage_complete"] is False
    assert out["summary"]["known_stop_risk_cash"] == 12
    assert len(out["by_symbol"]) == 2


def test_live_book_flags_stop_present_but_risk_calculation_missing():
    out = summarize_live_book(
        {"balance": 1000, "equity": 1000, "currency": "USD"},
        [{"ticket": 4, "symbol": "EURUSD", "side": "BUY", "volume": .1,
          "sl": 1.07, "price_open": 1.08, "stop_risk_cash": None,
          "stop_risk_cash_error": "broker calculator unavailable"}],
        [],
    )
    assert out["summary"]["positions_with_stop"] == 1
    assert out["summary"]["positions_with_stop_but_unpriced_risk"] == 1
    assert out["summary"]["stop_risk_coverage_complete"] is False
    assert out["summary"]["known_stop_risk_cash"] == 0


def test_closed_deal_performance_aggregates_legs_by_position_and_ignores_deposits():
    deals = [
        {"ticket": 1, "position_id": 50, "symbol": "EURUSD", "type": 0, "entry": 0,
         "profit": 0, "commission": -1, "swap": 0, "time": 100, "time_iso": "2026-01-01T00:00:00+00:00"},
        {"ticket": 2, "position_id": 50, "symbol": "EURUSD", "type": 1, "entry": 1,
         "profit": 25, "commission": -1, "swap": -2, "time": 200, "time_iso": "2026-01-02T00:00:00+00:00"},
        {"ticket": 3, "position_id": 51, "symbol": "XAUUSD", "type": 0, "entry": 0,
         "profit": 0, "commission": -1, "swap": 0, "time": 300, "time_iso": "2026-01-02T00:00:00+00:00"},
        {"ticket": 4, "position_id": 51, "symbol": "XAUUSD", "type": 1, "entry": 1,
         "profit": -10, "commission": -1, "swap": 0, "time": 400, "time_iso": "2026-01-03T00:00:00+00:00"},
        {"ticket": 5, "position_id": 0, "symbol": "", "type": 2, "entry": 0,
         "profit": 1000, "commission": 0, "swap": 0, "time": 500, "time_iso": "2026-01-03T00:00:00+00:00"},
    ]
    out = summarize_closed_deals(deals, days=90)
    assert out["trades"] == 2
    assert out["net_realized_pnl"] == pytest.approx(9)
    assert out["win_rate"] == pytest.approx(.5)
    assert len(out["daily_curve"]) == 2
    assert "not account equity" in out["notes"][0]


def test_stress_uses_broker_profit_callback_and_skips_unpriceable_positions():
    positions = [
        {"ticket": 1, "symbol": "EURUSD", "side": "BUY", "volume": 1, "price_current": 1.1},
        {"ticket": 2, "symbol": "XAUUSD", "side": "SELL", "volume": .1, "price_current": 2300},
        {"ticket": 3, "symbol": "BAD", "side": "BUY", "volume": 1, "price_current": 0},
    ]
    def calc(pos, target):
        return (target - pos["price_current"]) * (100 if pos["side"] == "BUY" else -100)
    out = build_stress_scenarios(positions, account_equity=5000, calc_profit=calc, shocks_pct=[-5, 5])
    assert out["scenarios"][0]["covered_positions"] == 2
    assert out["scenarios"][0]["unpriced_positions"] == 1
    assert out["scenarios"][0]["estimated_equity"] == pytest.approx(5000 + out["scenarios"][0]["incremental_pnl"])
    with pytest.raises(ValueError, match=r"between -50% and \+50%"):
        build_stress_scenarios([], account_equity=10, calc_profit=calc, shocks_pct=[60])


def test_rebalance_proposal_normalizes_small_rounding_and_never_executable():
    out = build_rebalance_proposal({"EURUSD": .6, "XAUUSD": .4}, {"EURUSD": .4, "GBPUSD": .6}, capital=10000, cost_bps_per_side=5)
    assert out["is_executable"] is False
    assert sum(r["target_weight"] for r in out["rows"]) == pytest.approx(1)
    assert {r["symbol"]: r["action"] for r in out["rows"]} == {"EURUSD": "INCREASE", "GBPUSD": "REDUCE", "XAUUSD": "INCREASE"}
    assert out["estimated_transaction_cost"] > 0
    with pytest.raises(ValueError, match="sum to between"):
        build_rebalance_proposal({"A": .2}, {"A": 1.0}, capital=1000, cost_bps_per_side=2)


def test_mt5_history_bridge_uses_documented_python_interval_api_not_history_select():
    from datetime import datetime
    from trading_team.data.mt5_bridge import MT5Bridge, MT5BridgeError

    class FakeMT5:
        def __init__(self):
            self.calls = []
        def terminal_info(self):
            return type("Terminal", (), {"connected": True})()
        def last_error(self):
            return (0, "ok")
        def history_deals_get(self, date_from, date_to, **kwargs):
            self.calls.append(("deals", date_from, date_to, kwargs))
            return [type("Deal", (), {"ticket": 1, "order": 2, "position_id": 3, "symbol": "EURUSD",
                    "type": 0, "entry": 0, "reason": 0, "volume": .1, "price": 1.1,
                    "profit": 0, "commission": -1, "swap": 0, "fee": 0, "magic": 0, "comment": "", "time": 100})()]
        def history_orders_get(self, date_from, date_to):
            self.calls.append(("orders", date_from, date_to, {}))
            return []

    fake = FakeMT5()
    bridge = MT5Bridge(fake)
    deals = bridge.history_deals(days=5)
    orders = bridge.history_orders(days=5)
    assert len(deals) == 1 and deals[0]["position_id"] == 3
    assert "fee" in deals[0]
    assert orders == []
    assert [x[0] for x in fake.calls] == ["deals", "orders"]


def test_portfolio_feature_tab_markup_and_routes_are_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    app = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    for tab in ("overview", "live", "allocation", "risk", "performance", "rebalance"):
        assert f'data-portfolio-tab="{tab}"' in html
        assert f'data-portfolio-panel="{tab}"' in html
    assert "async function loadPortfolioLive" in js
    assert "async function runPortfolioStress" in js
    assert "async function loadPortfolioPerformance" in js
    assert "async function buildPortfolioRebalance" in js
    for route in ("/api/portfolio/live", "/api/portfolio/performance", "/api/portfolio/analytics", "/api/portfolio/stress", "/api/portfolio/rebalance"):
        assert route in app
    assert "_sample_portfolio_account_snapshot()" in app
    assert "PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS" in app
    assert "async function loadPortfolioAnalytics" in js
    assert 'id="portfolioBenchmarkSymbol"' in html
    assert 'id="portfolioFactorProxySymbols"' in html


def test_portfolio_snapshot_store_samples_at_bounded_interval(tmp_path):
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    store = PortfolioSnapshotStore(tmp_path / "portfolio.db")
    acct = {"balance": 1000, "equity": 1020, "margin": 100, "margin_free": 920, "profit": 20, "currency": "USD"}
    assert store.record(acct, min_interval_seconds=60, now=1000) is True
    assert store.record({**acct, "equity": 1030}, min_interval_seconds=60, now=1020) is False
    assert store.record({**acct, "equity": 1040}, min_interval_seconds=60, now=1061) is True
    rows = store.list(days=1, limit=10, now=1062)
    assert len(rows) == 2
    assert rows[-1]["equity"] == 1040
    assert rows[0]["currency"] == "USD"


def test_portfolio_snapshots_are_partitioned_by_account(tmp_path):
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    store = PortfolioSnapshotStore(tmp_path / "portfolio.db")
    assert store.record({"login": 1001, "server": "Broker-A", "balance": 1000, "equity": 1000, "currency": "USD"}, now=1000)
    # A different account should not be throttled by the prior account's sample.
    assert store.record({"login": 2002, "server": "Broker-B", "balance": 2000, "equity": 2010, "currency": "EUR"}, now=1001)
    a = store.list(days=1, account_login=1001, now=1002)
    b = store.list(days=1, account_login=2002, now=1002)
    assert len(a) == len(b) == 1
    assert a[0]["currency"] == "USD"
    assert b[0]["currency"] == "EUR"


def test_portfolio_snapshot_store_migrates_older_schema_without_reset(tmp_path):
    import sqlite3
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE portfolio_account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, balance REAL NOT NULL,
            equity REAL NOT NULL, margin REAL NOT NULL, free_margin REAL NOT NULL,
            floating_profit REAL NOT NULL, currency TEXT NOT NULL DEFAULT '',
            account_server TEXT NOT NULL DEFAULT '')""")
        con.commit()
    store = PortfolioSnapshotStore(db)
    assert store.record({"login": 77, "balance": 1500, "equity": 1510, "currency": "USD"}, now=1000)
    rows = store.list(days=1, account_login=77, now=1001)
    assert len(rows) == 1 and rows[0]["equity"] == 1510
    with sqlite3.connect(db) as con:
        assert "account_login" in {r[1] for r in con.execute("PRAGMA table_info(portfolio_account_snapshots)")}


def test_performance_groups_completed_trades_by_magic_number():
    deals = [
        {"ticket": 10, "position_id": 101, "symbol": "EURUSD", "magic": 42, "type": 0, "entry": 0,
         "profit": 0, "commission": -1, "swap": 0, "time": 100, "time_iso": "2026-10-01T00:00:00+00:00"},
        {"ticket": 11, "position_id": 101, "symbol": "EURUSD", "magic": 42, "type": 1, "entry": 1,
         "profit": 11, "commission": -1, "swap": 0, "time": 200, "time_iso": "2026-10-02T00:00:00+00:00"},
        {"ticket": 12, "position_id": 202, "symbol": "XAUUSD", "magic": 99, "type": 0, "entry": 0,
         "profit": 0, "commission": -1, "swap": 0, "time": 300, "time_iso": "2026-10-02T00:00:00+00:00"},
        {"ticket": 13, "position_id": 202, "symbol": "XAUUSD", "magic": 99, "type": 1, "entry": 1,
         "profit": -8, "commission": -1, "swap": 0, "time": 400, "time_iso": "2026-10-03T00:00:00+00:00"},
    ]
    out = summarize_closed_deals(deals, days=90)
    by_magic = {x["magic"]: x for x in out["by_magic"]}
    assert by_magic[42]["net_pnl"] == pytest.approx(9)
    assert by_magic[99]["net_pnl"] == pytest.approx(-10)


def test_portfolio_snapshots_are_partitioned_by_login_and_server(tmp_path):
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    store = PortfolioSnapshotStore(tmp_path / "portfolio.db")
    assert store.record({"login": 1001, "server": "Broker-A", "balance": 1000, "equity": 1000, "currency": "USD"}, now=1000)
    # Same login on another server is a different account and must not be throttled or mixed.
    assert store.record({"login": 1001, "server": "Broker-B", "balance": 2000, "equity": 2010, "currency": "EUR"}, now=1001)
    a = store.list(days=1, account_login=1001, account_server="Broker-A", now=1002)
    b = store.list(days=1, account_login=1001, account_server="Broker-B", now=1002)
    assert len(a) == len(b) == 1
    assert a[0]["currency"] == "USD" and b[0]["currency"] == "EUR"
    assert "account_login" not in a[0] and "account_server" not in b[0]


def test_portfolio_snapshot_store_prunes_old_snapshots_by_configured_retention(tmp_path, monkeypatch):
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_RETENTION_DAYS", "30")
    store = PortfolioSnapshotStore(tmp_path / "retention.db")
    old = 1_700_000_000.0
    current = old + 31 * 86400
    assert store.record({"login": 1, "server": "A", "balance": 1000, "equity": 1000}, min_interval_seconds=0, now=old)
    assert store.record({"login": 1, "server": "A", "balance": 1100, "equity": 1110}, min_interval_seconds=0, now=current)
    rows = store.list(days=400, account_login=1, account_server="A", now=current + 1)
    assert len(rows) == 1
    assert rows[0]["equity"] == 1110


def test_portfolio_snapshot_list_samples_large_history_in_sql_and_preserves_endpoints(tmp_path):
    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
    store = PortfolioSnapshotStore(tmp_path / "sampling.db")
    start = 1_700_000_000.0
    for i in range(250):
        assert store.record({"login": 9, "server": "A", "balance": 1000 + i,
                             "equity": 1000 + i, "currency": "USD"},
                            min_interval_seconds=0, now=start + i * 61)
    rows = store.list(days=2, limit=17, account_login=9, account_server="A", now=start + 250 * 61)
    assert len(rows) <= 17
    assert rows[0]["equity"] == 1000
    assert rows[-1]["equity"] == 1249
    assert all(a["timestamp_epoch"] < b["timestamp_epoch"] for a, b in zip(rows, rows[1:]))
