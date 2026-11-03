from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/trading_team/web/static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
APP = (ROOT / "src/trading_team/web/app.py").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_release_versions_and_cache_busters_match():
    assert 'app.css?v=4.39.0' in HTML
    assert 'app.js?v=4.39.0' in HTML
    assert 'VERSION = "4.39.0"' in APP
    assert 'version = "4.39.0"' in PYPROJECT
    assert 'v4.39.0' in HTML


def test_user_palette_is_declared_and_both_themes_are_tinted():
    for color in ("#A7F432", "#0F1F16", "#EAF3E7", "#5AA9E6", "#25D162", "#1D1D1D", "#6C63FF"):
        assert color in CSS
    assert "body.theme-light, html[data-theme=\"light\"] body" in CSS
    assert "body:not(.theme-light)" in CSS


def test_market_tape_has_bounded_grid_and_vertical_label_value_layout():
    assert "grid-template-columns: minmax(0,1.08fr) minmax(0,1.08fr)" in CSS
    assert ".market-tape > .tape-cell" in CSS
    assert "flex-direction: column !important" in CSS
    assert ".tape-cell b { display: block !important" in CSS
    assert "overflow-wrap: anywhere !important" in CSS


def test_dashboard_kpis_have_explicit_semantic_classes_and_non_overflowing_values():
    assert 'class="hero-grid dashboard-kpis"' in HTML
    for klass in ("kpi-equity", "kpi-pnl", "kpi-margin", "kpi-cycle"):
        assert f"class=\"metric-card {klass}\"" in HTML
    assert ".dashboard-kpis > .metric-card .metric-main" in CSS
    assert "overflow-wrap: anywhere !important" in CSS


def test_optional_ui_sound_is_off_by_default_and_accessible():
    assert 'id="soundToggle"' in HTML
    assert 'aria-pressed="false"' in HTML
    assert "localStorage.getItem(SOUND_KEY) === 'on'" in JS
    assert "Subtle interface click sounds · off by default" in HTML
    assert "window.toggleUiSound" in JS
    assert "if (!soundIsEnabled()) return" in JS


def test_microinteractions_respect_reduced_motion():
    assert "@keyframes attPageEnter" in CSS
    assert ".btn:active:not(:disabled)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS


def test_news_and_trade_logic_are_not_modified_by_ui_patch():
    gate = (ROOT / "tests/test_news_directional_gate_v432.py").read_text(encoding="utf-8")
    assert "FLAT" in gate
    assert "market_relevance" in (ROOT / "src/trading_team/data/news_intelligence.py").read_text(encoding="utf-8")
