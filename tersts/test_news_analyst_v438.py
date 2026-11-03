from datetime import datetime, timezone, timedelta

from trading_team.agents.news import NewsAgent
from trading_team.agents.risk import RiskAgent, _news_risk_reduction
from trading_team.core.config import RiskConfig
from trading_team.core.events import EventTypes
from trading_team.core.models import Candle, MarketSnapshot, Side, TradePlan
from trading_team.data.news_analyst import NewsAnalystEngine


def _news(title, *, source="Reuters", domain="reuters.com", published=None, summary="", ident="a"):
    published = published or datetime.now(timezone.utc)
    return {
        "id": ident,
        "title": title,
        "summary": summary,
        "source": source,
        "provider": "RSS/Atom",
        "published": published.isoformat(),
        "url": f"https://www.{domain}/markets/{ident}",
    }


def _corroborated_gold_rates_story(now):
    title = "Federal Reserve cuts rates; gold rises as Treasury yields fall"
    body = "The Federal Reserve cut its policy rate. Gold prices rose as Treasury yields fell and the dollar weakened."
    return [
        _news(title, source="Reuters", domain="reuters.com", published=now - timedelta(minutes=12), summary=body, ident="fed-xau-r"),
        _news(title, source="Bloomberg", domain="bloomberg.com", published=now - timedelta(minutes=10), summary=body, ident="fed-xau-b"),
    ]


def test_news_analyst_blocks_celebrity_headline_even_if_geopolitical_tags_are_critical():
    now = datetime.now(timezone.utc)
    item = _news(
        "Kylian Mbappe faces China backlash as sponsors cut ties over viral meme",
        published=now - timedelta(minutes=4), summary="A viral meme triggered a sponsorship dispute.",
        ident="mbappe",
    )
    item["categories"] = ["geopolitics", "security"]
    item["urgency"] = "critical"
    report = NewsAnalystEngine(publish_events=False).analyze_feed([item], now=now, queue_llm=False)
    row = report["events"][0]
    assert row["event_type"] == "non_market_noise"
    assert row["market_relevance_score"] == 0
    assert row["agent_gate_status"] == "BLOCKED_NO_MARKET_LINK"
    assert row["event_impact_score"] <= 12
    assert row["explicit_exposures"] == []
    assert row["directional_vote_allowed"] is False
    assert row["order_authorization"] is False


def test_conflict_does_not_assume_gold_rises_and_oil_needs_supply_channel():
    now = datetime.now(timezone.utc)
    title = "Iran tensions threaten Strait of Hormuz oil shipping as gold and crude prices react"
    body = "Shipping disruption threatens crude supply. Gold is falling while the dollar strengthens and yields rise; oil prices jump."
    items = [
        _news(title, source="Reuters", domain="reuters.com", published=now - timedelta(minutes=5), summary=body, ident="iran-r"),
        _news(title, source="Bloomberg", domain="bloomberg.com", published=now - timedelta(minutes=4), summary=body, ident="iran-b"),
    ]
    report = NewsAnalystEngine(publish_events=False).analyze_feed(items, now=now, queue_llm=False)
    event = report["events"][0]
    hypotheses = {h["asset"]: h for h in event["asset_impact_hypotheses"]}
    assert event["event_type"] in {"geopolitical_shock", "commodity_supply"}
    assert hypotheses["GOLD"]["directional_state"] == "TWO_SIDED"
    assert "rising real yields" in hypotheses["GOLD"]["downside_channel"]
    assert hypotheses["OIL"]["directional_state"] == "CONDITIONAL_UP"
    assert report["policy"]["directional_vote_allowed"] is False


def test_fx_and_single_ticker_impacts_are_two_sided_and_need_market_confirmation():
    now = datetime.now(timezone.utc)
    fx = _news(
        "Bank of Japan signals policy shift; yen strengthens after yield move",
        published=now - timedelta(minutes=3), summary="JPY gains as the BOJ outlook changes.", ident="boj-jpy",
    )
    stock = _news(
        "$NVDA beats earnings; Nvidia shares surge after guidance",
        published=now - timedelta(minutes=3), summary="$NVDA revenue beats consensus, but forward guidance and margins remain uncertain.", ident="nvda",
    )
    engine = NewsAnalystEngine(publish_events=False)
    fx_row = engine.analyze_feed([fx], now=now, queue_llm=False)["events"][0]
    stock_row = engine.analyze_feed([stock], now=now, queue_llm=False)["events"][0]
    jpy = next(h for h in fx_row["asset_impact_hypotheses"] if h["asset"] == "JPY")
    assert jpy["directional_state"] == "TWO_SIDED"
    assert "rate differential" in jpy["downside_channel"] or "rate differentials" in jpy["downside_channel"]
    assert stock_row["ticker_mentions"] == ["NVDA"]
    assert stock_row["ticker_impact_hypotheses"][0]["ticker"] == "NVDA"
    assert stock_row["ticker_impact_hypotheses"][0]["directional_state"] == "TWO_SIDED"
    assert stock_row["directional_vote_allowed"] is False


def test_macro_surprise_is_not_inferred_when_consensus_comparison_is_missing():
    now = datetime.now(timezone.utc)
    item = _news(
        "US inflation report moves markets as yields fluctuate",
        published=now - timedelta(minutes=4),
        summary="Investors await details from the inflation report.", ident="cpi",
    )
    row = NewsAnalystEngine(publish_events=False).analyze_feed([item], now=now, queue_llm=False)["events"][0]
    assert row["explicit_surprise_status"] == "NOT_STATED_DO_NOT_INFER_SURPRISE"
    assert row["directional_vote_allowed"] is False


def test_same_instrument_post_publication_tape_is_measured_but_not_causally_attributed():
    now = datetime.now(timezone.utc)
    published = now - timedelta(minutes=13)
    items = _corroborated_gold_rates_story(now)
    candles = []
    closes = [4050.0, 4051.0, 4050.5, 4050.0, 4048.0, 4045.0, 4042.0, 4040.0]
    stamps = [now - timedelta(minutes=x) for x in (40, 35, 30, 25, 20, 15, 10, 5)]
    for stamp, close in zip(stamps, closes):
        candles.append(Candle(timestamp=stamp, open=close + 0.5, high=close + 1.0, low=close - 1.0, close=close))
    snapshot = MarketSnapshot(symbol="XAUUSD", price=4040.0, spread=0.2, candles={"M5": candles})
    report = NewsAnalystEngine(publish_events=False).analyze_instrument(items, "XAUUSD", snapshot=snapshot, now=now)
    assert report["qualified_event_count"] >= 1
    measured = [e for e in report["events"] if e.get("market_reaction", {}).get("status", "").startswith("OBSERVED_")]
    assert measured
    assert measured[0]["market_reaction"]["status"] == "OBSERVED_DOWN_MOVE"
    assert measured[0]["market_reaction"]["causal_attribution"] == "NOT_ESTABLISHED"
    gold = next(h for h in measured[0]["asset_impact_hypotheses"] if h["asset"] == "GOLD")
    assert gold["directional_state"] == "TWO_SIDED"
    assert report["directional_vote_allowed"] is False


def test_stale_candles_are_not_reported_as_current_market_reaction():
    now = datetime.now(timezone.utc)
    items = _corroborated_gold_rates_story(now)
    for item in items:
        item["published"] = (now - timedelta(minutes=90)).isoformat()
    offsets = (105, 100, 95, 85, 80, 75, 70, 65, 60)
    candles = [Candle(timestamp=now - timedelta(minutes=mins), open=100, high=101, low=99, close=100) for mins in offsets]
    snapshot = MarketSnapshot(symbol="XAUUSD", price=100, spread=0.1, candles={"M5": candles})
    report = NewsAnalystEngine(publish_events=False).analyze_instrument(items, "XAUUSD", snapshot=snapshot, now=now)
    statuses = {e.get("market_reaction", {}).get("status") for e in report["events"]}
    assert "STALE_MARKET_REACTION_DATA" in statuses


def test_news_agent_does_not_vote_directionally_from_analyst_scenarios():
    now = datetime.now(timezone.utc)
    items = _corroborated_gold_rates_story(now)
    analysis = NewsAnalystEngine(publish_events=False).analyze_instrument(items, "XAUUSD", now=now)
    snapshot = MarketSnapshot(
        symbol="XAUUSD", price=4000, spread=0.3,
        metadata={"news_analysis": analysis, "news_context": analysis.get("instrument_context", {}), "news_headlines": []},
    )
    signal = NewsAgent().analyze(snapshot)
    assert signal.side == Side.FLAT
    assert signal.score == 0
    assert any("NEWS DIRECTION BLOCKED" in reason for reason in signal.invalidations)


def test_news_risk_overlay_only_reduces_size_and_can_be_disabled(monkeypatch):
    now = datetime.now(timezone.utc)
    metadata = {"news_analysis": {"events": [{
        "event_id": "NWS-1", "headline": "Verified rate shock with observed XAUUSD move",
        "event_impact_score": 90, "evidence_quality_score": 82, "publisher_domain_count": 2,
        "eligible_for_instrument_research": True, "instrument_gate_status": "ELIGIBLE_CONTEXT_ONLY",
        "market_reaction": {"status": "OBSERVED_DOWN_MOVE", "move_in_pre_event_bar_ranges": -3.1, "as_of": now.isoformat()},
    }]}}
    snapshot = MarketSnapshot(symbol="XAUUSD", price=4000, spread=0.2, metadata=metadata)
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "1")
    scalar, reason = _news_risk_reduction(snapshot)
    assert scalar == 0.50
    assert "direction is unchanged" in reason
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "0")
    scalar, reason = _news_risk_reduction(snapshot)
    assert scalar == 1.0
    assert "no event-based size change" in reason


def test_risk_agent_actually_applies_the_one_way_size_reduction(monkeypatch):
    now = datetime.now(timezone.utc)
    metadata = {"news_analysis": {"events": [{
        "event_id": "NWS-1", "headline": "High impact verified rate shock",
        "event_impact_score": 90, "evidence_quality_score": 82, "publisher_domain_count": 2,
        "eligible_for_instrument_research": True, "instrument_gate_status": "ELIGIBLE_CONTEXT_ONLY",
        "market_reaction": {"status": "OBSERVED_DOWN_MOVE", "move_in_pre_event_bar_ranges": -3.1, "as_of": now.isoformat()},
    }]}}
    snapshot = MarketSnapshot(symbol="XAUUSD", price=4000, spread=0.2, metadata=metadata)
    plan = TradePlan(symbol="XAUUSD", side=Side.BUY, entry=4000, stop=3995, target=4010, confidence=0.8)
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "1")
    reduced = RiskAgent(RiskConfig()).evaluate(snapshot, plan, equity=10000, daily_pnl=0, exposure=0, open_positions=0)
    assert reduced.approved is True
    assert round(reduced.quantity, 6) == 3.2
    assert round(reduced.max_loss, 6) == 16.0
    size_check = next(row for row in reduced.checks if row["code"] == "POSITION_SIZE")
    assert "news_risk_scalar=0.50" in size_check["detail"]
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "0")
    unmodified = RiskAgent(RiskConfig()).evaluate(snapshot, plan, equity=10000, daily_pnl=0, exposure=0, open_positions=0)
    assert round(unmodified.quantity, 6) == 6.4
    assert round(unmodified.max_loss, 6) == 32.0


def test_risk_overlay_refuses_low_quality_or_single_domain_story(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("NEWS_RISK_REDUCTION_ENABLED", "1")
    snapshot = MarketSnapshot(symbol="XAUUSD", price=4000, spread=0.2, metadata={"news_analysis": {"events": [{
        "event_impact_score": 95, "evidence_quality_score": 20, "publisher_domain_count": 1,
        "eligible_for_instrument_research": True, "instrument_gate_status": "ELIGIBLE_CONTEXT_ONLY",
        "market_reaction": {"status": "OBSERVED_DOWN_MOVE", "move_in_pre_event_bar_ranges": -4.0, "as_of": now.isoformat()},
    }]}})
    scalar, _ = _news_risk_reduction(snapshot)
    assert scalar == 1.0


def test_news_analyst_broadcasts_reports_to_internal_event_bus(monkeypatch):
    now = datetime.now(timezone.utc)
    emitted = []
    def fake_publish(event_type, payload=None, *, source=""):
        emitted.append((event_type, payload or {}, source))
    monkeypatch.setattr("trading_team.data.news_analyst.publish", fake_publish)
    items = _corroborated_gold_rates_story(now)
    engine = NewsAnalystEngine(publish_events=True)
    engine.analyze_feed(items, now=now, queue_llm=False)
    engine.analyze_instrument(items, "XAUUSD", now=now)
    event_types = {row[0] for row in emitted}
    assert EventTypes.NEWS_EVENT_ANALYSED in event_types
    assert EventTypes.NEWS_FEED_REPORT in event_types
    assert EventTypes.NEWS_INSTRUMENT_REPORT in event_types
    assert EventTypes.NEWS_RISK_CONTEXT in event_types
    assert all(payload.get("directional_vote_allowed") is False for typ, payload, _ in emitted if typ == EventTypes.NEWS_EVENT_ANALYSED)


def test_ollama_remote_endpoint_is_blocked_and_disabled_by_default(monkeypatch):
    monkeypatch.setenv("NEWS_ANALYST_OLLAMA_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://example.com")
    blocked = NewsAnalystEngine(publish_events=False)
    assert blocked.ollama_status["status"] == "blocked_non_loopback_endpoint"
    monkeypatch.setenv("NEWS_ANALYST_OLLAMA_ENABLED", "0")
    disabled = NewsAnalystEngine(publish_events=False)
    assert disabled.ollama_status["status"] == "disabled"


def test_news_api_and_newsroom_ui_surface_agent_analysis():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    app = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    html = (root / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    assert '"instrument_analysis": instrument_analysis' in app
    assert 'row["news_analyst"] = match' in app
    assert 'id="newsAgentConsole"' in html
    assert "function renderNewsAgentConsole()" in js
    assert "item.news_analyst" in js


def test_news_api_attaches_item_and_instrument_analyst_reports(monkeypatch):
    from types import SimpleNamespace
    from trading_team.web import app as web_app

    now = datetime.now(timezone.utc)
    items = _corroborated_gold_rates_story(now)
    engine = NewsAnalystEngine(publish_events=False)
    instrument_report = engine.analyze_instrument(items, "XAUUSD", now=now)

    class Provider:
        enabled = True
        configured_count = 2
        last_items = []
        last_status = {"reuters": {"status": "ok", "count": 1}}
        last_fetch_at = now.isoformat()
        last_fetch_epoch = 0
        last_error = None
        refreshing = False
        gdelt_enabled = True
        newsapi_enabled = False
        newsapi_key_configured = False
        newsapi_mode = "everything"
        def status(self):
            return {"enabled": True, "configured_feeds": 2, "rss_feeds": 2, "last_fetch_at": self.last_fetch_at,
                    "refreshing": False, "last_error": None, "feeds": self.last_status}

    class Runtime:
        news_provider = Provider()
        news_items = items
        news_analyst = engine
        last_snapshot = MarketSnapshot(symbol="XAUUSD", price=4000, spread=0.2, metadata={"news_analysis": instrument_report})
        config = SimpleNamespace(symbol="XAUUSD")

    monkeypatch.setattr(web_app, "RUNTIME", Runtime())
    handler = web_app.Handler.__new__(web_app.Handler)
    handler.path = "/api/news?limit=10"
    captured = {}
    handler.send_json = lambda payload, code=200: captured.update(payload=payload, status=code)
    handler.do_GET()
    assert captured["status"] == 200
    payload = captured["payload"]
    assert payload["impact_analysis"]["engine"] == "instrument_aware_news_impact_v1"
    assert payload["instrument_analysis"]["instrument"] == "XAUUSD"
    assert any(isinstance(row.get("news_analyst"), dict) for row in payload["items"])
