from __future__ import annotations
from datetime import datetime, timezone
import time
import sqlite3

from trading_team.research.trade_history_learning import TradeHistoryLearningStore, set_active_account_scope


def _deal(ticket, position_id, symbol, typ, entry, stamp, *, pnl=0.0, commission=0.0, swap=0.0, fee=0.0, magic=17):
    return {
        "ticket": ticket, "order": ticket + 10000, "position_id": position_id, "symbol": symbol,
        "type": typ, "entry": entry, "reason": 0, "volume": 0.1, "price": 1.1 + ticket / 100000,
        "profit": pnl, "commission": commission, "swap": swap, "fee": fee, "magic": magic,
        "comment": "test", "time": stamp, "time_iso": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
    }


def test_mt5_deals_are_idempotent_and_net_costs_are_aggregated(tmp_path):
    store = TradeHistoryLearningStore(tmp_path / "history.db")
    now = int(time.time())
    deals = [
        _deal(1, 91, "EURUSD", 0, 0, now - 1000, commission=-1.0),
        _deal(2, 91, "EURUSD", 1, 1, now - 500, pnl=20.0, commission=-1.0, swap=-0.2, fee=-0.1),
        _deal(3, 0, "", 2, 0, now - 400, pnl=500.0),  # deposit/balance operation is not a trade
    ]
    first = store.sync(deals, account_login=123, account_server="Demo-Server", requested_days=90)
    second = store.sync(deals, account_login=123, account_server="Demo-Server", requested_days=90)
    assert first["sync"]["new_deals"] == 3
    assert second["sync"]["new_deals"] == 0
    assert second["sync"]["updated_existing_deals"] == 3
    summary = store.summary(days=365)
    assert summary["deals_stored"] == 3
    assert summary["summary"]["closed_trades"] == 1
    assert summary["summary"]["net_pnl"] == 17.7
    assert summary["by_symbol"][0]["symbol"] == "EURUSD"


def test_currently_open_and_incomplete_history_are_excluded_from_learning_stats(tmp_path):
    store = TradeHistoryLearningStore(tmp_path / "history.db")
    now = int(time.time())
    deals = [
        _deal(10, 100, "XAUUSD", 0, 0, now - 900, commission=-1),
        _deal(11, 100, "XAUUSD", 1, 1, now - 800, pnl=5),
        _deal(12, 101, "XAUUSD", 0, 0, now - 700, commission=-1),
        _deal(13, 101, "XAUUSD", 1, 1, now - 600, pnl=8),
        _deal(14, 102, "EURUSD", 1, 1, now - 500, pnl=50),  # no retained opening leg
    ]
    store.sync(deals, account_login=123, account_server="Demo-Server", requested_days=90, open_position_ids=[100])
    summary = store.summary(days=365)
    assert summary["summary"]["closed_trades"] == 1
    assert summary["summary"]["net_pnl"] == 7.0
    assert summary["open_positions"] == 1
    assert summary["incomplete_closed_positions"] == 1


def test_learning_context_is_point_in_time_and_withholds_small_sample_metrics(tmp_path):
    store = TradeHistoryLearningStore(tmp_path / "history.db")
    now = int(time.time())
    deals = []
    ticket = 1000
    for i in range(12):
        pid = 200 + i
        close_stamp = now - 600 + i * 10 if i < 10 else now + 3600 + i * 10
        deals.append(_deal(ticket, pid, "GBPUSD", 0, 0, close_stamp - 20, commission=-0.1, magic=33)); ticket += 1
        deals.append(_deal(ticket, pid, "GBPUSD", 1, 1, close_stamp, pnl=(2.0 if i % 2 == 0 else -1.0), commission=-0.1, magic=33)); ticket += 1
    store.sync(deals, account_login=456, account_server="Broker-X", requested_days=90)
    ctx = store.learning_context("GBPUSD", as_of=datetime.fromtimestamp(now, tz=timezone.utc), minimum_sample=10)
    assert ctx["status"] == "usable"
    assert ctx["sample_count"] == 10  # two future outcomes are excluded
    assert ctx["win_rate"] == 0.5
    ctx_small = store.learning_context("GBPUSD", as_of=datetime.fromtimestamp(now, tz=timezone.utc), minimum_sample=11)
    assert ctx_small["status"] == "insufficient_sample"
    assert "win_rate" not in ctx_small


def test_learning_context_does_not_leak_another_mt5_account(tmp_path):
    store = TradeHistoryLearningStore(tmp_path / "history.db")
    now = int(time.time())
    deals = []
    for i in range(12):
        pid = 500 + i
        deals += [_deal(2000 + i * 2, pid, "EURUSD", 0, 0, now - 500 + i * 10),
                  _deal(2001 + i * 2, pid, "EURUSD", 1, 1, now - 495 + i * 10, pnl=2.0)]
    store.sync(deals, account_login=111, account_server="Broker-A", requested_days=30)
    set_active_account_scope(222, "Broker-B")
    ctx = store.learning_context("EURUSD", as_of=datetime.now(timezone.utc))
    assert ctx["sample_count"] == 0
    assert ctx["account_scope"] == "222@Broker-B"
    # Clear global process scope so other tests remain isolated.
    set_active_account_scope(None, None)


def test_inout_netting_reversal_is_not_misreported_as_one_complete_trade(tmp_path):
    set_active_account_scope(None, None)
    store = TradeHistoryLearningStore(tmp_path / "history.db")
    now = int(time.time())
    deals = [
        _deal(1, 600, "EURUSD", 0, 0, now - 300, commission=-0.2),
        _deal(2, 600, "EURUSD", 1, 2, now - 200, pnl=3.0, commission=-0.2),
        _deal(3, 600, "EURUSD", 0, 1, now - 100, pnl=4.0, commission=-0.2),
    ]
    store.sync(deals, account_login=1, account_server="Netting", requested_days=30)
    summary = store.summary(days=365)
    assert summary["summary"]["closed_trades"] == 0
    assert summary["incomplete_closed_positions"] == 1



def test_trade_learning_routes_ui_and_agent_context_are_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    app_src = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    quant_src = (root / "src/trading_team/research/quant_context.py").read_text(encoding="utf-8")
    prompt_src = (root / "src/trading_team/ai_mesh/context.py").read_text(encoding="utf-8")
    js_src = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    html_src = (root / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    assert '"/api/trade-learning/summary"' in app_src
    assert '"/api/trade-learning/sync"' in app_src
    assert '"historical_trade_learning"' in quant_src
    assert "Historical closed-trade outcomes" in prompt_src
    assert "syncTradeHistoryAndLearn" in js_src
    assert 'id="tradeHistoryLearningPanel"' in html_src


def test_decision_attribution_is_account_scoped_before_learning(tmp_path):
    from trading_team.storage.performance import PerformanceStore

    db_path = tmp_path / "shared.db"
    set_active_account_scope(111, "Broker-A")
    perf = PerformanceStore(str(db_path))
    now = int(time.time())
    perf.record_decision("decision-a", "EURUSD", "BUY", 0.7, 1.1, 1.09, 1.12, [], meta_features=None)
    perf.attach_position("decision-a", "11")
    columns = {r[1] for r in perf.db.execute("PRAGMA table_info(decision_attribution)").fetchall()}
    assert {"account_login", "account_server"} <= columns
    scoped = perf.db.execute("SELECT account_login,account_server FROM decision_attribution WHERE client_id='decision-a'").fetchone()
    assert tuple(scoped) == ("111", "Broker-A")
    perf.db.close()

    deals = [_deal(11, 700, "EURUSD", 0, 0, now - 20), _deal(12, 700, "EURUSD", 1, 1, now - 10, pnl=4.0)]
    store = TradeHistoryLearningStore(db_path)
    store.sync(deals, account_login=111, account_server="Broker-A", requested_days=30)
    summary_a = store.summary(days=365)
    assert summary_a["summary"]["closed_trades"] == 1
    assert summary_a["recent_trades"][0]["decision_matched"] == 1

    # Same ticket and symbol on another broker account must not reuse the old decision row.
    set_active_account_scope(222, "Broker-B")
    store.sync(deals, account_login=222, account_server="Broker-B", requested_days=30)
    summary_b = store.summary(days=365)
    assert summary_b["account"] == {"login": "222", "server": "Broker-B"}
    assert summary_b["recent_trades"][0]["decision_matched"] == 0
    set_active_account_scope(None, None)


def test_legacy_decision_table_migrates_without_losing_history(tmp_path):
    from trading_team.storage.performance import PerformanceStore

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE decision_attribution (
            client_id TEXT PRIMARY KEY, created TEXT NOT NULL, symbol TEXT, decision TEXT,
            confidence REAL, entry REAL, stop REAL, target REAL, signals_json TEXT,
            position_ticket TEXT, closed INTEGER DEFAULT 0, realized_pnl REAL, realized_r REAL,
            outcome TEXT, closed_at TEXT, meta_features_json TEXT)""")
        db.execute("INSERT INTO decision_attribution(client_id,created,symbol,decision) VALUES(?,?,?,?)",
                   ("legacy-row", datetime.now(timezone.utc).isoformat(), "EURUSD", "BUY"))
    perf = PerformanceStore(str(db_path))
    cols = {r[1] for r in perf.db.execute("PRAGMA table_info(decision_attribution)").fetchall()}
    row = perf.db.execute("SELECT client_id,account_login,account_server FROM decision_attribution WHERE client_id='legacy-row'").fetchone()
    assert {"account_login", "account_server"} <= cols
    assert tuple(row) == ("legacy-row", None, None)
    perf.db.close()
