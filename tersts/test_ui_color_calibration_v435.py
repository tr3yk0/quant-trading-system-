from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/trading_team/web/static/app.css").read_text(encoding="utf-8")
HTML = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "src/trading_team/web/app.py").read_text(encoding="utf-8")
JS = (ROOT / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_v435_runtime_version_and_cache_busters_match():
    assert 'app.css?v=4.39.0' in HTML
    assert 'app.js?v=4.39.0' in HTML
    assert 'VERSION = "4.39.0"' in APP
    assert "version = \"4.39.0\"" in PYPROJECT
    assert "s.version || '4.39.0'" in JS


def test_both_themes_use_coloured_non_monochrome_workspace_palettes():
    assert "--bg-deep: #111a29" in CSS
    assert "--bg-deep: #e7edf5" in CSS
    assert "--accent-violet: #aa9aee" in CSS
    assert "--accent-teal: #58c9ad" in CSS
    assert "--accent-amber: #e4b565" in CSS
    assert "--accent-coral: #ed8c86" in CSS
    assert "linear-gradient(135deg, #e5edf6 0%, #eff3f8 48%, #e4edf5 100%)" in CSS


def test_metrics_have_consistent_semantic_colour_assignments_in_both_themes():
    for color in ("#6ebaf0", "#58c9ad", "#aa9aee", "#e4b565", "#2d85c4", "#219b7d", "#8875d1", "#c28a3d"):
        assert color in CSS
    assert "news-intel-metric:nth-child(1)" in CSS
    assert "news-intel-metric:nth-child(4)" in CSS


def test_news_sections_and_semantic_statuses_have_distinct_design_roles():
    for selector in (".news-event-radar", ".news-asset-pressure", ".news-coverage-brief", ".news-impact-pill.impact-critical", ".news-tag.sentiment-positive", ".news-tag.sentiment-negative", ".status-pill.good", ".status-pill.warn", ".status-pill.bad"):
        assert selector in CSS


def test_v432_news_risk_gate_remains_separate_from_ui_colour_pass():
    gate = (ROOT / "tests/test_news_directional_gate_v432.py").read_text(encoding="utf-8")
    intelligence = (ROOT / "src/trading_team/data/news_intelligence.py").read_text(encoding="utf-8")
    assert "FLAT" in gate
    assert "market_relevance" in intelligence
    assert "research_priority" in intelligence
