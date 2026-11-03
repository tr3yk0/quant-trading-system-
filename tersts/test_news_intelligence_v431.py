from datetime import datetime, timezone, timedelta

from trading_team.data.news_intelligence import build_news_intelligence, enrich_news_items

NOW = datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc)


def test_high_impact_fresh_macro_headline_has_explainable_priority():
    rows = enrich_news_items([{
        "title": "Federal Reserve signals rate cut after CPI inflation cools",
        "summary": "US Treasury yields fall as inflation eases.",
        "source": "Reuters",
        "provider": "RSS/Atom",
        "published": (NOW - timedelta(minutes=12)).isoformat(),
        "categories": ["macro", "central_banks"],
        "url": "https://example.com/story",
    }], now=NOW)
    row = rows[0]
    assert 0 <= row["research_priority"] <= 100
    assert row["event_type"] == "central_bank"
    assert row["impact_band"] in {"high", "critical"}
    assert row["source_quality_score"] >= 80
    assert row["freshness_score"] >= 90
    assert {x["key"] for x in row["asset_exposures"]} >= {"USD", "RATES"}
    assert row["research_priority_reasons"]
    assert row["event_cluster_id"].startswith("EVT-")


def test_near_duplicate_headlines_cluster_and_corroboration_is_visible():
    rows = [
        {"title": "Federal Reserve signals rate cut after inflation cools", "source": "Reuters", "published": NOW.isoformat()},
        {"title": "Federal Reserve signals rate cut after inflation cools", "source": "Bloomberg", "published": NOW.isoformat()},
        {"title": "Local sports team wins final", "source": "Unknown", "published": NOW.isoformat()},
    ]
    intel = build_news_intelligence(rows, now=NOW)
    enriched = enrich_news_items(rows, now=NOW)
    assert enriched[0]["event_cluster_id"] == enriched[1]["event_cluster_id"]
    assert enriched[0]["event_cluster_size"] == 2
    assert intel["duplicate_headline_count"] == 1
    assert intel["cross_source_corroborated_clusters"] >= 1
    assert intel["trade_authorization"] is False
    assert intel["is_predictive_model"] is False


def test_unknown_or_missing_dates_are_safe_and_do_not_receive_freshness_credit():
    rows = enrich_news_items([
        {"title": "Company reports quarterly earnings", "source": "Unknown", "published": "not-a-time"},
        {"title": "New macro report released", "source": "Unrated desk"},
    ], now=NOW)
    assert len(rows) == 2
    assert all(0 <= row["research_priority"] <= 100 for row in rows)
    assert rows[0]["freshness_minutes"] is None
    assert rows[0]["freshness_score"] == 0
    assert rows[1]["source_quality_tier"] == "unrated_label"


def test_source_tier_is_a_heuristic_not_a_fact_check_and_outputs_are_copies():
    original = {"title": "Markets edge higher", "source": "Reuters", "published": NOW.isoformat()}
    enriched = enrich_news_items([original], now=NOW)
    assert "research_priority" not in original
    assert enriched[0]["source_quality_tier"] == "tier_1_label"
    intel = build_news_intelligence(enriched, now=NOW)
    assert intel["engine"] == "conservative_news_linkage_v2"
    assert any("not verified accuracy" in text for text in intel["methodology"])


def test_asset_matrix_is_sorted_by_coverage_and_headline_tone_is_bounded():
    rows = [
        {"title": "Gold jumps as US dollar falls and Treasury yields decline", "source": "Reuters", "published": NOW.isoformat(), "local_sentiment_score": 0.7},
        {"title": "Oil supply fears rise after OPEC meeting", "source": "CNBC", "published": NOW.isoformat(), "local_sentiment_score": -0.3},
    ]
    intel = build_news_intelligence(rows, now=NOW)
    assert intel["headline_count"] == 2
    assert intel["mean_novelty_score"] >= 0
    assert all(-1 <= row["tone_score"] <= 1 for row in intel["asset_matrix"])
    keys = {row["key"] for row in intel["asset_matrix"]}
    assert {"GOLD", "USD", "RATES", "OIL"} <= keys


def test_institutional_terminal_ui_and_api_wiring_are_present():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    app = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    html = (root / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    assert "build_news_intelligence, enrich_news_items" in app
    assert '"intelligence": intelligence' in app
    assert 'id="newsEventRadarList"' in html
    assert 'id="newsAssetMatrix"' in html
    assert 'id="newsIntelMethodology"' in html
    assert "function renderNewsIntelligence()" in js
    assert "function setNewsImpactFilter(value)" in js
    assert "function setNewsAssetFilter(value)" in js
