from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/trading_team/web/static'
CSS = (STATIC / 'app.css').read_text(encoding='utf-8')
JS = (STATIC / 'app.js').read_text(encoding='utf-8')
HTML = (STATIC / 'index.html').read_text(encoding='utf-8')
APP = (ROOT / 'src/trading_team/web/app.py').read_text(encoding='utf-8')
PYPROJECT = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')


def test_theme_background_restores_blue_slate_dark_and_light_modes():
    assert 'background-color: #111a29 !important' in CSS
    assert 'linear-gradient(145deg, #111a29 0%, #172338 48%, #14283a 100%)' in CSS
    assert 'background-color: #e7edf5 !important' in CSS
    assert 'linear-gradient(135deg, #e5edf6 0%, #eff3f8 48%, #e4edf5 100%)' in CSS
    # Green remains a semantic color, not the whole workspace background.
    assert 'background: #0F1F16 !important' in CSS  # success badge surface
    assert 'background: #EAF3E7 !important' in CSS  # light success badge surface


def test_requested_palette_retained_for_semantic_accents():
    for color in ('#A7F432', '#0F1F16', '#EAF3E7', '#5AA9E6', '#25D162', '#1D1D1D', '#6C63FF'):
        assert color in CSS
    assert 'border-left: 3px solid #A7F432' in CSS
    assert '--accent: #5AA9E6 !important' in CSS
    assert '--accent-2: #6C63FF !important' in CSS


def test_theme_switch_has_short_origin_sweep_and_respects_reduced_motion():
    assert 'html.theme-wipe::after' in CSS
    assert 'animation: attThemeSweep 330ms' in CSS
    assert '@media (prefers-reduced-motion: reduce)' in CSS
    assert "root.style.setProperty('--att-theme-x'" in JS
    assert "root.style.setProperty('--att-theme-y'" in JS
    assert "root.classList.add('theme-wipe')" in JS
    assert 'themeSwitchTimers.forEach(timer => clearTimeout(timer))' in JS
    assert "}, 350)" in JS


def test_v437_asset_cache_busters_and_release_versions_align():
    assert 'app.css?v=4.39.0' in HTML
    assert 'app.js?v=4.39.0' in HTML
    assert 'VERSION = "4.39.0"' in APP
    assert 'version = "4.39.0"' in PYPROJECT
    assert 'v4.39.0' in HTML
    assert "s.version || '4.39.0'" in JS
    assert "s.version || '4.39.0'} · PORT" in JS
