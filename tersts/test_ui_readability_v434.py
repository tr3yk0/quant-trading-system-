from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src/trading_team/web/static/app.css"
HTML = ROOT / "src/trading_team/web/static/index.html"
APP = ROOT / "src/trading_team/web/app.py"
PYPROJECT = ROOT / "pyproject.toml"


def test_v433_cache_keys_and_runtime_version_are_aligned():
    html = HTML.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert 'app.css?v=4.39.0' in html
    assert 'app.js?v=4.39.0' in html
    assert 'VERSION = "4.39.0"' in app
    assert 'version = "4.39.0"' in pyproject


def test_v433_news_ui_has_contrast_and_readability_rules_for_both_themes():
    css = CSS.read_text(encoding="utf-8")
    assert "v4.34.0 — manually composed institutional terminal" in css
    assert "--text-muted: #c0cde1" in css
    assert "body.theme-light .news-intel-panel" in css
    assert "body:not(.theme-light) .news-intel-panel" in css
    assert ".news-radar-body > strong { font-size: 13px" in css
    assert ".news-intel-metric > small { font-size: 11px" in css
    assert ":focus-visible" in css
    assert "@media (max-width: 430px)" in css


def test_news_priority_logic_is_unchanged_by_ui_release():
    intelligence = (ROOT / "src/trading_team/data/news_intelligence.py").read_text(encoding="utf-8")
    assert "market_relevance" in intelligence
    assert "research_priority" in intelligence
    gate_tests = (ROOT / "tests/test_news_directional_gate_v432.py").read_text(encoding="utf-8")
    assert "FLAT" in gate_tests
