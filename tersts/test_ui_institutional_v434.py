from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/trading_team/web/static/app.css").read_text(encoding="utf-8")
HTML = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "src/trading_team/web/app.py").read_text(encoding="utf-8")


def test_v434_release_and_asset_cache_keys_match():
    assert 'app.css?v=4.39.0' in HTML
    assert 'app.js?v=4.39.0' in HTML
    assert 'VERSION = "4.39.0"' in APP


def test_institutional_skin_removes_ambient_ai_style_decoration():
    assert "v4.34.0 — manually composed institutional terminal" in CSS
    assert ".main::before, .main::after { display: none !important" in CSS
    assert ".brandmark" in CSS and "animation: none !important" in CSS
    assert ".global-nav { display: none !important; }" in CSS
    assert ".metric-card::before, .metric-card::after { display: none !important" in CSS
    assert "body:not(.theme-light) .sidebar::before { display: none !important" in CSS


def test_dark_and_light_themes_have_distinct_explicit_surface_palettes():
    assert "--bg-deep: #0c1219" in CSS
    assert "--bg-deep: #edf1f5" in CSS
    assert "body.theme-light .news-intel-panel" in CSS
    assert "html[data-theme=\"light\"] .sidebar" in CSS
    assert "html[data-theme=\"light\"] #sidebar .side-nav button.nav-item.active" in CSS


def test_status_colour_palette_and_focus_state_remain_present():
    assert ".status-pill.good" in CSS
    assert ".status-pill.warn" in CSS
    assert ".status-pill.bad" in CSS
    assert ":focus-visible" in CSS


def test_news_directional_risk_gate_source_is_preserved():
    gate = (ROOT / "tests/test_news_directional_gate_v432.py").read_text(encoding="utf-8")
    intelligence = (ROOT / "src/trading_team/data/news_intelligence.py").read_text(encoding="utf-8")
    assert "FLAT" in gate
    assert "market_relevance" in intelligence
    assert "research_priority" in intelligence
