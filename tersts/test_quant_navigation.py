from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "src/trading_team/web/static/app.css").read_text(encoding="utf-8")

def test_portfolio_is_dedicated_main_page_and_navigation_target():
    assert re.search(r'<section[^>]+class="page"[^>]+id="portfolio"', HTML)
    assert re.search(r'class="nav-item"[^>]+data-page="portfolio"', HTML)
    assert re.search(r'class="global-nav-btn"[^>]+data-page="portfolio"', HTML)
    assert "portfolio:'Portfolio Allocation & Risk'" in JS
    assert "if(stage === 'portfolio')" in JS and "goPage('portfolio'" in JS

def test_portfolio_workflow_is_not_embedded_in_quant_page():
    # The dedicated workspace owns the portfolio panel; Quant's row provides a route to it.
    assert 'id="quantPortfolio"' not in HTML
    assert 'id="portfolioWorkspace"' in HTML
    assert 'id="quantPortfolioSymbols"' in HTML

def test_quant_stage_buttons_have_real_matching_panels():
    stages = re.findall(r'class="stage-tab(?: active)?"[^>]*data-stage="([^"]+)"', HTML)
    panels = re.findall(r'data-stage-panel="([^"]+)"', HTML)
    stage_names = {s for s in stages if s != "portfolio"}
    assert stage_names == set(panels)
    assert 'id="quantStagePanels"' in HTML
    assert 'p.hidden = !selected' in JS
    assert '.stage-panel[hidden] { display: none !important; }' in CSS

def test_each_quant_stage_has_separate_content_owner():
    expected = ["quantHero", "chartKalman", "chartVol", "chartPricing", "chartStrat", "chartWF", "quantForecast", "quantDocs", "chartIce", "quantGridBody"]
    for element_id in expected:
        assert HTML.count(f'id="{element_id}"') == 1, element_id

def test_asset_cache_bust_and_version_are_updated():
    assert 'app.js?v=4.39.0' in HTML
    assert 'app.css?v=4.39.0' in HTML
    assert 'VERSION = "4.39.0"' in (ROOT / "src/trading_team/web/app.py").read_text(encoding="utf-8")
