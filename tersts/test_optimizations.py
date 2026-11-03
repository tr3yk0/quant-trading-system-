from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from trading_team.core.async_runtime import AsyncMarketRuntime
from trading_team.core.resilience import BoundedTTLCache, retry_call
from trading_team.market.registry import InstrumentRegistry
from trading_team.storage.orchestration import DurableScanQueue


def test_bounded_cache_evicts_oldest_and_can_serve_stale_on_error():
    cache = BoundedTTLCache(max_entries=2, default_stale_ttl=1.0)
    cache.set("one", 1, ttl=10)
    cache.set("two", 2, ttl=10)
    cache.set("three", 3, ttl=10)
    assert cache.get("one") is None
    assert cache.get("two") == 2
    assert cache.stats()["evictions"] == 1

    cache.set("stale", {"ok": True}, ttl=0.01, stale_ttl=1.0)
    time.sleep(0.025)
    assert cache.get("stale") is None
    assert cache.get("stale", allow_stale=True) == {"ok": True}
    value = cache.get_or_create("stale", 1.0, lambda: (_ for _ in ()).throw(RuntimeError("temporary")))
    assert value == {"ok": True}
    assert cache.stats()["stale_hits"] >= 2


def test_retry_call_retries_transient_error_without_unbounded_wait():
    calls = {"n": 0}

    def sometimes_fails():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("transient")
        return "ok"

    assert retry_call(sometimes_fails, attempts=3, base_delay=0, max_delay=0) == "ok"
    assert calls["n"] == 3


def test_async_runtime_processes_entire_universe_in_bounded_waves(monkeypatch):
    monkeypatch.setenv("ASYNC_RETRY_BASE_DELAY", "0")
    active = 0
    max_active = 0
    lock = threading.Lock()
    calls: dict[str, int] = {}

    async def handler(symbol: str):
        nonlocal active, max_active
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol == "SYM000" and calls[symbol] == 1:
            raise RuntimeError("transient first attempt")
        with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.001)
        with lock:
            active -= 1
        return {"symbol": symbol, "ok": True}

    runtime = AsyncMarketRuntime(handler, max_concurrency=3, batch_size=7, retries=1)
    symbols = [f"sym{i:03d}" for i in range(50)]
    results = asyncio.run(runtime.run_once(symbols))
    assert len(results) == 50
    assert all(item.ok for item in results)
    assert max_active <= 3
    assert calls["SYM000"] == 2
    assert len({item.symbol for item in results}) == 50

    # Task-batch limit is respected even if configured concurrency is higher.
    active2 = 0
    max_active2 = 0

    async def narrow_handler(symbol: str):
        nonlocal active2, max_active2
        active2 += 1
        max_active2 = max(max_active2, active2)
        await asyncio.sleep(0.001)
        active2 -= 1
        return {"symbol": symbol}

    narrow = AsyncMarketRuntime(narrow_handler, max_concurrency=8, batch_size=2, retries=0)
    assert len(asyncio.run(narrow.run_once([f"x{i}" for i in range(8)]))) == 8
    assert max_active2 <= 2


def test_universe_registry_unlimited_catalogue_and_pagination(tmp_path: Path):
    db_path = str(tmp_path / "universe.db")
    registry = InstrumentRegistry(db_path)
    try:
        rows = [
            {"name": "EURUSD", "description": "Euro vs US Dollar", "path": "Forex\\Majors",
             "currency_base": "EUR", "currency_profit": "USD", "trade_mode": 4},
            {"name": "XAUUSD", "description": "Gold vs US Dollar", "path": "Metals\\Spot",
             "currency_base": "XAU", "currency_profit": "USD", "trade_mode": 4},
            {"name": "US500", "description": "US 500 Index", "path": "Indices\\US",
             "currency_profit": "USD", "trade_mode": 4},
        ]
        registry.sync(rows, complete_snapshot=True, source_total=len(rows), source_id="test")
        assert len(registry.candidates(limit=None, include_disabled=True)) == 3
        assert set(registry.symbol_names(limit=None, include_disabled=True)) == {"EURUSD", "XAUUSD", "US500"}
        assert registry.count() == 3
        original_first_seen = registry.get("EURUSD")["first_seen_at"]
        repeated = registry.sync(rows, complete_snapshot=True, source_total=len(rows), source_id="test-repeat")
        assert repeated["inserted"] == 0
        assert repeated["updated"] == 3
        assert registry.get("EURUSD")["first_seen_at"] == original_first_seen
        page1 = registry.list(limit=2, offset=0)
        page2 = registry.list(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1
    finally:
        registry.close()


def test_durable_scan_results_batch_reads_and_pages(tmp_path: Path):
    queue = DurableScanQueue(str(tmp_path / "queue.db"))
    try:
        for symbol in ("EURUSD", "XAUUSD", "US500"):
            queue.enqueue(job_key=f"test:{symbol}", symbol=symbol, payload={"symbol": symbol})
            job = queue.claim_next("test-worker")
            assert job is not None
            assert queue.complete(job["id"], {
                "symbol": symbol, "status": "SUCCEEDED", "candidate_status": "NO_SETUP",
                "candidate_side": "FLAT", "quote_fresh": True, "scan_completed": True,
            }, worker_id="test-worker")
        result_map = queue.result_map(["EURUSD", "XAUUSD", "US500", "missing"])
        assert set(result_map) == {"EURUSD", "XAUUSD", "US500"}
        result_state = queue.result_state_map(["EURUSD", "US500", "missing"])
        assert set(result_state) == {"EURUSD", "US500"}
        assert result_state["EURUSD"]["status"] == "SUCCEEDED"
        assert queue.pending_symbols() == set()
        page1 = queue.results_page(page=1, page_size=2)
        page2 = queue.results_page(page=2, page_size=2)
        assert page1["total"] == 3
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 1
        assert page1["has_next"] is True
        assert page2["has_previous"] is True
    finally:
        queue.close()


def test_claim_batch_uses_one_bounded_batch_and_distinct_leases(tmp_path: Path):
    queue = DurableScanQueue(str(tmp_path / "claim-batch.db"))
    try:
        for i in range(5):
            queue.enqueue(job_key=f"batch:{i}", symbol=f"SYM{i}", payload={"n": i}, priority=5-i)
        first = queue.claim_batch("worker-a", batch_size=3)
        second = queue.claim_batch("worker-b", batch_size=3)
        assert len(first) == 3
        assert len(second) == 2
        assert len({job["id"] for job in first + second}) == 5
        assert all(job["status"] == "RUNNING" for job in first + second)
        assert {job["worker_id"] for job in first} == {"worker-a"}
        assert {job["worker_id"] for job in second} == {"worker-b"}
    finally:
        queue.close()


def test_candidate_results_are_filtered_and_paginated(tmp_path: Path):
    queue = DurableScanQueue(str(tmp_path / "candidate-page.db"))
    try:
        fixtures = [
            ("EURUSD", "CANDIDATE", "BUY", True),
            ("XAUUSD", "NO_SETUP", "FLAT", True),
            ("US500", "CANDIDATE", "SELL", False),
        ]
        for symbol, candidate_status, side, quote_fresh in fixtures:
            queue.enqueue(job_key=f"page:{symbol}", symbol=symbol, payload={"symbol": symbol})
            job = queue.claim_next("candidate-test")
            assert job is not None
            assert queue.complete(job["id"], {
                "symbol": symbol, "status": "SUCCEEDED", "candidate_status": candidate_status,
                "candidate_side": side, "quote_fresh": quote_fresh, "scan_completed": True,
            }, worker_id="candidate-test")
        page = queue.candidate_results_page(page=1, page_size=1)
        assert page["total"] == 1
        assert len(page["items"]) == 1
        assert page["items"][0]["symbol"] == "EURUSD"
        assert page["has_next"] is False
    finally:
        queue.close()


def test_positions_and_orders_stores_support_server_pagination(tmp_path: Path):
    from trading_team.storage.positions import PositionStore
    from trading_team.storage.orders import OrderStore

    db_path = str(tmp_path / "paged-trades.db")
    positions = PositionStore(db_path)
    orders = OrderStore(db_path)
    try:
        positions.replace_all([
            {"ticket": 1, "symbol": "EURUSD", "side": "BUY", "volume": 0.1},
            {"ticket": 2, "symbol": "XAUUSD", "side": "SELL", "volume": 0.2},
            {"ticket": 3, "symbol": "US500", "side": "BUY", "volume": 0.3},
        ])
        assert positions.count() == 3
        assert len(positions.list(limit=2, offset=0)) == 2
        assert len(positions.list(limit=2, offset=2)) == 1

        for i, symbol in enumerate(("EURUSD", "XAUUSD", "US500")):
            orders.create(f"order-{i}", symbol, "BUY", 0.1, 1.0, 0.9, 1.2)
        assert orders.count() == 3
        assert len(orders.list(limit=2, offset=0)) == 2
        assert len(orders.list(limit=2, offset=2)) == 1
    finally:
        positions.db.close()
        orders.db.close()


def test_mt5_rates_retry_transient_empty_history(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from trading_team.data.mt5_bridge import MT5Bridge
    import trading_team.data.mt5_bridge as bridge_module

    dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
             ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4")]
    bars = np.array([(1_760_000_000, 1.0, 1.1, 0.9, 1.05, 100, 2),
                     (1_760_000_300, 1.05, 1.2, 1.0, 1.15, 120, 2)], dtype=dtype)

    class FakeMT5:
        TIMEFRAME_M5 = 5
        def __init__(self): self.calls = 0
        def terminal_info(self): return SimpleNamespace(connected=True)
        def symbol_info(self, name): return SimpleNamespace(name=name) if name == "EURUSD" else None
        def symbol_select(self, name, enabled): return True
        def last_error(self): return (1, "history synchronizing")
        def copy_rates_from_pos(self, name, timeframe, start, count):
            self.calls += 1
            return None if self.calls == 1 else bars
        def copy_rates_from(self, *args): return None
        def copy_rates_range(self, *args): return None

    monkeypatch.setattr(bridge_module.time, "sleep", lambda *_: None)
    bridge = MT5Bridge(FakeMT5())
    result = bridge.rates("EURUSD", "M5", count=10)
    assert len(result) == 2
    assert result[-1]["close"] == 1.15
    assert bridge.mt5.calls == 2


def test_mt5_rates_empty_history_has_actionable_error(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from trading_team.data.mt5_bridge import MT5Bridge, MT5BridgeError
    import trading_team.data.mt5_bridge as bridge_module

    dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
             ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4")]
    empty = np.array([], dtype=dtype)

    class FakeMT5:
        TIMEFRAME_H1 = 16385
        def terminal_info(self): return SimpleNamespace(connected=True)
        def symbol_info(self, name): return SimpleNamespace(name=name) if name == "EURUSD" else None
        def symbol_select(self, name, enabled): return True
        def last_error(self): return (4401, "history not found")
        def copy_rates_from_pos(self, *args): return empty
        def copy_rates_from(self, *args): return empty
        def copy_rates_range(self, *args): return empty

    monkeypatch.setattr(bridge_module.time, "sleep", lambda *_: None)
    bridge = MT5Bridge(FakeMT5())
    try:
        bridge.rates("EURUSD", "H1", count=10)
        raise AssertionError("Expected empty history to fail explicitly")
    except MT5BridgeError as exc:
        message = str(exc)
        assert "EURUSD/H1" in message
        assert "Max. bars in chart" in message


def test_local_ai_provider_defaults_to_deterministic_when_not_opted_in(monkeypatch):
    monkeypatch.delenv("LOCAL_AI_ENABLED", raising=False)
    monkeypatch.delenv("LOCAL_AI_TIERED", raising=False)
    from trading_team.ai_mesh.provider import create_provider, DeterministicProvider
    assert isinstance(create_provider(), DeterministicProvider)


def test_data_feed_history_uses_retrying_bridge(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from trading_team.data.mt5 import MT5DataFeed
    import trading_team.data.mt5_bridge as bridge_module

    dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
             ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4")]
    bars = np.array([(1_760_001_000, 1.0, 1.1, 0.9, 1.05, 80, 1)], dtype=dtype)

    class FakeMT5:
        TIMEFRAME_H1 = 16385
        def terminal_info(self): return SimpleNamespace(connected=True)
        def symbol_info(self, name): return SimpleNamespace(name=name) if name == "AUDJPY" else None
        def symbol_select(self, name, enabled): return True
        def last_error(self): return (1, "history synchronizing")
        def copy_rates_from_pos(self, *args): return bars
        def copy_rates_from(self, *args): return bars
        def copy_rates_range(self, *args): return bars

    monkeypatch.setattr(bridge_module.time, "sleep", lambda *_: None)
    feed = MT5DataFeed(auto_connect=False)
    feed.mt5 = FakeMT5()
    feed.bridge.attach(feed.mt5, api_lock=feed._api_lock)
    monkeypatch.setattr(feed, "ensure_connected", lambda: True)
    candles = feed.history("AUDJPY", "H1", bars=11)
    assert len(candles) == 1
    assert candles[0].close == 1.05
    assert candles[0].timestamp.tzinfo is not None


def test_local_ai_enrichment_runs_off_hot_path_and_is_bounded(monkeypatch):
    """Enabling local AI must not make a market-analysis call wait for Ollama."""
    import threading
    import time as _time
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from trading_team.ai_mesh import hybrid_analyst as hybrid
    from trading_team.core.models import Side

    monkeypatch.setenv("LOCAL_AI_ENABLED", "true")
    monkeypatch.setenv("HYBRID_LLM_ENRICH", "true")
    monkeypatch.setenv("HYBRID_LLM_MAX_DESKS", "1")
    monkeypatch.setenv("HYBRID_LLM_INCLUDE_NON_VOTING", "false")
    monkeypatch.setenv("LOCAL_AI_SYMBOL_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("LOCAL_AI_GLOBAL_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("LOCAL_AI_MAX_PENDING_ENRICHMENTS", "2")

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_fake(_pack, _provider, *, desk_prompt=None):
        calls.append(desk_prompt)
        started.set()
        release.wait(1.5)
        return {"ok": True, "explanation": "background result", "conviction": 0.8,
                "direction": "BUY", "observation_vs_hypothesis": [], "non_voting": False}

    monkeypatch.setattr(hybrid, "enrich_with_llm", slow_fake)
    symbol = "ASYNC_LOCAL_AI_REGRESSION"
    stamp = datetime(2026, 10, 25, tzinfo=timezone.utc)
    snapshot = SimpleNamespace(
        symbol=symbol, price=1.25, spread=0.0001, metadata={}, timestamp=stamp,
        candles={"M5": [SimpleNamespace(timestamp=stamp, close=1.25)]},
    )
    reports = [
        SimpleNamespace(agent=f"DESK_{i}", role="test", side=Side.BUY, confidence=.65,
                        score=.5, thesis="classical", evidence=[f"evidence {i}"],
                        invalidations=[], data_quality=.9, non_voting=False)
        for i in range(5)
    ]
    t0 = _time.monotonic()
    returned = hybrid.hybrid_enrich_reports(reports, snapshot, SimpleNamespace(name="fake", model="fake"), max_desks=None)
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.25
    assert len(returned) == len(reports)
    # One desk is scheduled for this symbol/scan, not one call per report.
    assert started.wait(0.5)
    assert len(calls) == 1
    release.set()


def test_local_ai_advisory_does_not_mutate_trade_decision_by_default(monkeypatch):
    from types import SimpleNamespace
    from trading_team.ai_mesh.hybrid_analyst import apply_enrichment_to_report
    from trading_team.core.models import Side

    monkeypatch.setenv("LOCAL_AI_CAN_INFLUENCE_DECISION", "false")
    report = SimpleNamespace(
        side=Side.BUY, score=.62, confidence=.67, thesis="classical thesis",
        evidence=["closed bar confirmed"], invalidations=[],
    )
    result = apply_enrichment_to_report(report, {
        "ok": True, "explanation": "advisory wording", "conviction": .9,
        "direction": "WAIT", "observation_vs_hypothesis": [], "non_voting": False,
        "confirmation_trigger": "", "invalidation_note": "",
    })
    assert result.side == Side.BUY
    assert result.score == .62
    assert result.confidence == .67
    assert "advisory wording" in result.thesis


def test_continuous_local_ai_cannot_block_guardian_or_chair_by_default(monkeypatch):
    """Unset continuous LLM flags are safe even when LOCAL_AI_ENABLED is turned on."""
    from pathlib import Path
    from trading_team.ai_mesh import continuous_flow

    monkeypatch.setenv("LOCAL_AI_ENABLED", "true")
    monkeypatch.delenv("CONTINUOUS_GUARDIAN_LLM", raising=False)
    monkeypatch.delenv("CONTINUOUS_CHAIR_LLM", raising=False)
    assert continuous_flow._env_bool("CONTINUOUS_GUARDIAN_LLM", False) is False
    assert continuous_flow._env_bool("CONTINUOUS_CHAIR_LLM", False) is False


def test_local_ai_provider_status_does_not_probe_network(monkeypatch):
    """Settings apply and dashboard status must not wait on /models probes."""
    monkeypatch.setenv("LOCAL_AI_ENABLED", "true")
    from trading_team.ai_mesh.provider import LocalOpenAIProvider, TieredLocalAIProvider

    single = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="test", timeout=90)
    monkeypatch.setattr(single, "list_models", lambda: (_ for _ in ()).throw(AssertionError("network probe")))
    status = single.status()
    assert status["probe_state"] == "not_probed"
    assert single.timeout <= 30

    tiered = TieredLocalAIProvider()
    monkeypatch.setattr(tiered.gate, "list_models", lambda: (_ for _ in ()).throw(AssertionError("gate probe")))
    monkeypatch.setattr(tiered.deep, "list_models", lambda: (_ for _ in ()).throw(AssertionError("deep probe")))
    tiered_status = tiered.status()
    assert tiered_status["mode"] == "tiered_sequential"
    assert tiered_status["models_seen"] == []


def test_mt5_rates_recovers_after_forced_symbol_reselection(monkeypatch):
    """History can become available only after MT5 re-adds the symbol to Market Watch."""
    import numpy as np
    from types import SimpleNamespace
    from trading_team.data.mt5_bridge import MT5Bridge
    import trading_team.data.mt5_bridge as bridge_module

    dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
             ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4")]
    empty = np.array([], dtype=dtype)
    bars = np.array([(1_760_001_000, 1.0, 1.1, 0.9, 1.05, 80, 1)], dtype=dtype)

    class FakeMT5:
        TIMEFRAME_M5 = 5
        def __init__(self):
            self.select_calls = 0
        def terminal_info(self): return SimpleNamespace(connected=True)
        def symbol_info(self, name): return SimpleNamespace(name=name) if name == "RESELECT_TEST_SYMBOL" else None
        def symbol_select(self, name, enabled):
            if enabled:
                self.select_calls += 1
            return True
        def last_error(self): return (4401, "history synchronizing")
        def copy_rates_from_pos(self, *args):
            return bars if self.select_calls >= 2 else empty
        def copy_rates_from(self, *args): return empty
        def copy_rates_range(self, *args): return empty

    monkeypatch.setattr(bridge_module.time, "sleep", lambda *_: None)
    fake = FakeMT5()
    bridge = MT5Bridge(fake)
    result = bridge.rates("RESELECT_TEST_SYMBOL", "M5", count=10)
    assert len(result) == 1
    assert result[0]["close"] == 1.05
    assert fake.select_calls >= 2


def test_quant_page_auto_preview_and_chart_errors_are_visible():
    repo = Path(__file__).resolve().parents[1]
    app_js = (repo / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    index_html = (repo / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    backend = (repo / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    assert "ensureQuantPreview" in app_js
    assert app_js.count("async function runUnifiedQuant") == 1
    assert "Promise.allSettled([quantPromise, icePromise])" in app_js
    assert "Quant request failed" in app_js
    assert "Chart library unavailable" in app_js
    assert 'app.js?v=4.39.0' in index_html
    assert 'app.css?v=4.39.0' in index_html
    assert 'result["data_source"] = data_source' in backend
    assert 'MARKET_HISTORY_UNAVAILABLE' in backend
    assert 'Never synthesize a price path' in backend
    assert '_parsed_json_body_available' in backend


def test_quant_service_preview_generates_expected_chart_payloads():
    from trading_team.research.quant_lab.service import run_quant_lab

    closes = [2000.0 + i * 0.05 + ((i % 7) - 3) * 0.2 for i in range(400)]
    result = run_quant_lab(
        closes=closes,
        stages=["kalman", "garch", "ewma", "forecast", "vol_surface", "signals", "walk_forward"],
    )
    assert result["n_bars"] == 400
    assert result["charts"]["price_kalman"]["close"]
    assert result["charts"]["vol_surface"]["iv_grid"]
    assert result["charts"]["strategies"]
    assert result["charts"]["walk_forward"]["fold"]


def test_post_forwarded_request_reuses_parsed_json_body():
    """The shared GET handler must see the body parsed by do_POST."""
    from trading_team.web.request_utils import read_json_body

    class NoReadStream:
        def read(self, *_args, **_kwargs):
            raise AssertionError("forwarded POST must not consume the HTTP body twice")

    class FakeHandler:
        headers = {"Content-Length": "37"}
        rfile = NoReadStream()
        _parsed_json_body_available = True
        _parsed_json_body = {
            "symbol": "EURUSD", "timeframe": "H1", "stages": ["kalman"], "bars": 125
        }

    assert read_json_body(FakeHandler()) == FakeHandler._parsed_json_body


def test_quant_service_labels_assumed_volatility_as_scenario_not_market_iv():
    import numpy as np
    from trading_team.research.quant_lab.service import run_quant_lab

    closes = (100.0 * np.exp(np.cumsum(np.full(80, 0.001)))).tolist()
    result = run_quant_lab(closes=closes, stages=["vol_surface"])
    surface = result["stages"]["vol_surface"]
    assert surface["market_implied"] is False
    assert surface["surface_type"] == "model_scenario"
    assert "no option chain quotes" in surface["source_note"].lower()
    assert len(result["charts"]["vol_surface"]["iv_grid"]) == 3


def test_quant_service_does_not_invent_market_closes_when_history_is_missing():
    from trading_team.research.quant_lab.service import run_quant_lab

    result = run_quant_lab(candles=[], stages=["kalman"])
    assert result["n_bars"] == 0
    assert result["errors"]["data"] == "Need at least 30 closes"
    assert result["charts"] == {}


def test_ice_bridge_accepts_runtime_candle_dataclasses():
    from datetime import datetime, timedelta, timezone
    from trading_team.core.models import Candle
    from trading_team.strategies.ice_grid.bridge import IceGridBridge

    candles = []
    price = 100.0
    now = datetime.now(timezone.utc) - timedelta(minutes=5 * 80)
    for i in range(80):
        close = price * (1.0 + ((i % 5) - 2) * 0.0005)
        candles.append(Candle(
            timestamp=now + timedelta(minutes=5 * i),
            open=price, high=max(price, close) * 1.001,
            low=min(price, close) * 0.999, close=close, volume=100 + i,
        ))
        price = close
    result = IceGridBridge(default_grid="ATRAdaptive").evaluate_bars(candles)
    assert result["backend"] in {"python", "native"}
    assert "ice" in result and "grid" in result
    assert len(result["ice"]["layers"]) >= 1
    assert result["grid"]["levels"]


def test_quant_ui_ids_are_unique_and_feature_stage_targets_exist():
    from collections import Counter
    from html.parser import HTMLParser

    repo = Path(__file__).resolve().parents[1]
    index_html = (repo / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    app_js = (repo / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")

    class IdParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
        def handle_starttag(self, tag, attrs):
            value = dict(attrs).get("id")
            if value:
                self.ids.append(value)

    parser = IdParser()
    parser.feed(index_html)
    counts = Counter(parser.ids)
    assert not [element_id for element_id, count in counts.items() if count > 1]
    for element_id in ("chartKalman", "chartIce", "chartVol", "chartPricing", "quantForecast", "portfolioWorkspace", "quantDocs"):
        assert element_id in counts
    # Rendering is bound to plot IDs and the docs sink; the forecast section itself is a layout container.
    for element_id in ("chartKalman", "chartIce", "chartVol", "chartPricing", "chartForecast", "quantDocs"):
        assert f"'{element_id}'" in app_js or f'"{element_id}"' in app_js
