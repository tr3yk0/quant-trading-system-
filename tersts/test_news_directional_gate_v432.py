from datetime import datetime, timezone, timedelta

from trading_team.core.models import MarketSnapshot, Side
from trading_team.data.news_intelligence import (
    assess_news_for_instrument,
    build_news_intelligence,
    enrich_news_items,
    validate_directional_news_signal,
)
from trading_team.agents.news import NewsAgent
from trading_team.ai_mesh.analysts import _deterministic

NOW = datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)


def _item(title, **kwargs):
    row = {
        "title": title,
        "summary": kwargs.pop("summary", ""),
        "source": kwargs.pop("source", "Reuters"),
        "provider": kwargs.pop("provider", "RSS/Atom"),
        "published": kwargs.pop("published", NOW.isoformat()),
        "url": kwargs.pop("url", "https://www.reuters.com/markets/example"),
    }
    row.update(kwargs)
    return row


def test_mbappe_meme_headline_cannot_become_geopolitical_trading_event():
    headline = _item(
        "Kylian Mbappé faces China backlash as sponsors cut ties over viral meme",
        source="Bloomberg Markets",
        categories=["geopolitics", "security", "world"],
        urgency="critical",
    )
    enriched = enrich_news_items([headline], now=NOW)[0]

    assert enriched["event_type"] == "non_market_noise"
    assert enriched["market_relevance_score"] == 0
    assert enriched["asset_exposures"] == []
    assert enriched["research_priority"] <= 12
    assert enriched["impact_band"] == "low"
    assert enriched["trade_gate_status"] == "BLOCKED_NO_MARKET_LINK"
    assert enriched["priority_is_probability"] is False

    intel = build_news_intelligence([headline], now=NOW)
    assert intel["market_relevant_headline_count"] == 0
    assert intel["blocked_unlinked_headline_count"] == 1
    assert intel["trade_authorization"] is False


def test_china_name_alone_does_not_create_yuan_or_risk_exposure():
    row = enrich_news_items([_item("Celebrity receives backlash after viral meme in China")], now=NOW)[0]
    assert "CNY" not in {x["key"] for x in row["asset_exposures"]}
    assert row["market_relevance_score"] == 0


def test_fresh_reuters_fed_story_is_linked_to_eurusd_but_context_only():
    story = _item(
        "Federal Reserve signals rate cut after CPI inflation cools",
        summary="US Treasury yields fall as inflation eases.",
        source="Reuters",
        published=(NOW - timedelta(minutes=12)).isoformat(),
    )
    independent_pickup = _item(
        "Federal Reserve signals rate cut after CPI inflation cools",
        summary="US Treasury yields fall as inflation eases.",
        source="Bloomberg",
        published=(NOW - timedelta(minutes=10)).isoformat(),
        url="https://www.bloomberg.com/markets/example",
    )
    assessment = assess_news_for_instrument([story, independent_pickup], "EURUSD", now=NOW)
    assert assessment["instrument"] == "EURUSD"
    assert assessment["candidate_headline_count"] == 2
    assert assessment["corroboration_gate_passed"] is True
    assert assessment["market_relevance_score"] >= 60
    assert assessment["directional_vote_allowed"] is False
    assert assessment["trade_authorization"] is False


def test_stale_or_untraceable_market_story_is_excluded_from_agent_context():
    stale = _item(
        "Federal Reserve announces emergency rate decision",
        published=(NOW - timedelta(hours=10)).isoformat(),
        url="",
    )
    assessment = assess_news_for_instrument([stale], "EURUSD", now=NOW)
    assert assessment["candidate_headline_count"] == 0
    assert assessment["matched_headline_count"] >= 1
    assert assessment["status"] == "BLOCKED_NO_VERIFIED_FRESH_INSTRUMENT_LINK"


def test_raw_provider_bias_and_urgency_are_not_enough_for_directional_vote():
    snap = MarketSnapshot(
        symbol="EURUSD", price=1.08, spread=0.0001,
        metadata={
            "news_bias": 0.95,
            "news_impact": "CRITICAL",
            "news_provider_connected": True,
            "news_bias_source": "example-feed",
            "news_headlines": [{"title": "Kylian Mbappé faces China backlash over viral meme"}],
        },
        candles={},
    )
    classic = NewsAgent().analyze(snap)
    mesh = _deterministic("NEWS_ANALYST", snap)
    assert classic.side == Side.FLAT and classic.score == 0
    assert mesh.side == Side.FLAT and mesh.score == 0 and mesh.non_voting is True


def test_directional_news_model_requires_oos_provenance_and_fresh_price_reaction(monkeypatch):
    report_hash = "a" * 64
    monkeypatch.setenv("NEWS_ALPHA_DIRECTIONAL_ENABLED", "1")
    monkeypatch.setenv("NEWS_ALPHA_APPROVED_MODEL_ID", "institutional-news-alpha")
    monkeypatch.setenv("NEWS_ALPHA_APPROVED_MODEL_VERSION", "2.3.1")
    monkeypatch.setenv("NEWS_ALPHA_VALIDATION_REPORT_SHA256", report_hash)
    valid_meta = {
        "news_bias": 0.5,
        "news_bias_source": "institutional-news-alpha",
        "news_bias_method": "event-surprise-model",
        "news_bias_model_id": "institutional-news-alpha",
        "news_bias_model_version": "2.3.1",
        "news_bias_validation_report_sha256": report_hash,
        "news_bias_instrument": "EURUSD",
        "news_bias_horizon_minutes": 15,
        "news_bias_validation_status": "VALIDATED_OOS",
        "news_bias_calibrated": True,
        "news_bias_confidence": 0.68,
        "news_bias_oos_after_costs": True,
        "news_bias_oos_sample_size": 500,
        "news_bias_oos_net_expectancy": 0.12,
        "news_bias_timestamp": NOW.isoformat(),
        "news_provider_connected": True,
        "news_market_reaction_confirmed": True,
        "news_market_reaction_instrument": "EURUSD",
        "news_market_reaction_score": 0.45,
        "news_market_reaction_timestamp": NOW.isoformat(),
        "news_context": {
            "instrument": "EURUSD",
            "candidate_headline_count": 2,
            "market_relevance_score": 85,
            "corroboration_gate_passed": True,
        },
        "news_headlines": [{
            "title": "Federal Reserve signals rate cut after CPI inflation cools",
            "source": "Reuters", "published": NOW.isoformat(),
        }],
    }
    ok, reasons = validate_directional_news_signal(valid_meta, "EURUSD", now=NOW)
    assert ok, reasons

    conflict = dict(valid_meta)
    conflict["news_market_reaction_score"] = -0.45
    ok, reasons = validate_directional_news_signal(conflict, "EURUSD", now=NOW)
    assert not ok
    assert any("conflicts" in r for r in reasons)



def test_directional_news_model_metadata_cannot_self_approve(monkeypatch):
    monkeypatch.delenv("NEWS_ALPHA_DIRECTIONAL_ENABLED", raising=False)
    monkeypatch.delenv("NEWS_ALPHA_APPROVED_MODEL_ID", raising=False)
    monkeypatch.delenv("NEWS_ALPHA_APPROVED_MODEL_VERSION", raising=False)
    monkeypatch.delenv("NEWS_ALPHA_VALIDATION_REPORT_SHA256", raising=False)
    meta = {
        "news_bias": 0.8,
        "news_bias_source": "raw-rss-sentiment",
        "news_bias_method": "headline-lexicon",
        "news_bias_model_id": "self-declared-model",
        "news_bias_model_version": "999",
        "news_bias_validation_report_sha256": "b" * 64,
        "news_bias_instrument": "EURUSD",
        "news_bias_horizon_minutes": 15,
        "news_bias_validation_status": "VALIDATED_OOS",
        "news_bias_calibrated": True,
        "news_bias_confidence": 0.99,
        "news_bias_oos_after_costs": True,
        "news_bias_oos_sample_size": 10000,
        "news_bias_oos_net_expectancy": 99,
        "news_bias_timestamp": NOW.isoformat(),
        "news_provider_connected": True,
        "news_market_reaction_confirmed": True,
        "news_market_reaction_instrument": "EURUSD",
        "news_market_reaction_score": 0.9,
        "news_market_reaction_timestamp": NOW.isoformat(),
        "news_context": {
            "instrument": "EURUSD", "candidate_headline_count": 2,
            "market_relevance_score": 90, "corroboration_gate_passed": True,
        },
        "news_headlines": [{"title": "Federal Reserve rate decision", "source": "Reuters", "published": NOW.isoformat()}],
    }
    ok, reasons = validate_directional_news_signal(meta, "EURUSD", now=NOW)
    assert not ok
    assert any("disabled by default" in reason for reason in reasons)
    assert any("operator approval pins" in reason for reason in reasons)


def test_two_labels_on_one_publisher_domain_do_not_count_as_independent_corroboration():
    first = _item(
        "Federal Reserve signals rate cut after CPI inflation cools",
        source="Reuters", url="https://markets.example.com/story-a",
    )
    second = _item(
        "Federal Reserve signals rate cut after CPI inflation cools",
        source="Bloomberg", url="https://markets.example.com/story-b",
    )
    assessment = assess_news_for_instrument([first, second], "EURUSD", now=NOW)
    assert assessment["matched_headline_count"] == 2
    assert assessment["candidate_headline_count"] == 0
    assert assessment["rejected_corroboration_count"] >= 1
    assert assessment["corroboration_gate_passed"] is False

def test_an_unvalidated_news_bias_is_flat_in_both_agent_paths():
    # The symbol link is present, but no validated news-alpha model/price reaction is declared.
    story = _item("Federal Reserve signals rate cut; EURUSD reacts", summary="EURUSD rises after the Fed statement.")
    context = assess_news_for_instrument([story], "EURUSD", now=NOW)
    snap = MarketSnapshot(
        symbol="EURUSD", price=1.08, spread=0.0001,
        metadata={
            "news_bias": 0.9,
            "news_bias_source": "raw-rss-sentiment",
            "news_provider_connected": True,
            "news_context": context,
            "news_headlines": context["candidate_headlines"],
        },
        candles={},
    )
    assert NewsAgent().analyze(snap).side == Side.FLAT
    report = _deterministic("NEWS_ANALYST", snap)
    assert report.side == Side.FLAT
    assert report.non_voting is True
    assert any("news" in x.lower() for x in report.invalidations)
