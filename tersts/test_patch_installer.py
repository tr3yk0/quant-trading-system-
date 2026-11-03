from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

PATCH_ROOT = Path(__file__).resolve().parents[1]


def _fixture_project(root: Path) -> None:
    (root / "src/trading_team/data").mkdir(parents=True)
    (root / "src/trading_team/web/static").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copy2(PATCH_ROOT / "apply_tradingnews_press_v429.py", root)
    shutil.copy2(PATCH_ROOT / "src/trading_team/data/tradingnews_press.py", root / "src/trading_team/data")
    (root / "src/trading_team/data/news_rss.py").write_text('''from typing import Any
class RSSNewsProvider:
    def __init__(self):
        self.urls = {}
        self.gdelt_enabled = True
        self.newsapi_enabled = False
        self.newsapi_mode = "everything"
        self.newsapi_key_configured = False
        self._load_newsapi_state()
        self.last_status = {}
    def _load_newsapi_state(self): pass
    @property
    def configured_count(self) -> int:
        return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled)
    def status(self):
        return {
            "newsapi_mode": self.newsapi_mode,
            "feeds": self.last_status,
        }
    def refresh_async(self):
        if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled:
            return False
    def fetch(self):
        items = []
        statuses = {}
        # De-duplicate by canonical URL; fall back to normalized headline for URL variants.
        deduped: dict[str, dict] = {}
        return items
''', encoding="utf-8")
    (root / "src/trading_team/web/app.py").write_text('''VERSION = "4.28.0"\nnews = {"newsapi_mode": getattr(RUNTIME.news_provider, "newsapi_mode", "everything"),}\n''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.js").write_text('''const version = '4.28.0';
setTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.gdelt_enabled?'GDELT on':'GDELT off'} · ${NewsHubState.status?.newsapi_enabled?'NewsAPI on':NewsHubState.status?.newsapi_key_configured?'NewsAPI disabled':'NewsAPI key not set'}`);
    const cats=(item.categories||[]).slice(0,3).map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join('');
    return `<div class="news-tag-row">${cats}<span class="news-provider-tag">${esc(item.provider||'Feed')}</span></div>`;
''', encoding="utf-8")
    (root / "src/trading_team/web/static/index.html").write_text('''<link href="app.css?v=4.28.0"><script src="app.js?v=4.28.0"></script><span id="brandVersion">v4.28.0</span>''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.css").write_text("/* CSS */\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('version = "4.28.0"\n', encoding="utf-8")
    (root / "START.bat").write_text('@echo off\necho AI Trading Team v4.28.0\n', encoding="utf-8")
    (root / ".env.example").write_text('NEWS_RSS_ENABLED=1\n', encoding="utf-8")
    (root / "README.md").write_text('# AI Trading Team v4.28.0\n', encoding="utf-8")


def test_installer_patches_v428_layout_and_is_idempotent(tmp_path):
    root = tmp_path / "project"
    _fixture_project(root)
    script = root / "apply_tradingnews_press_v429.py"
    one = subprocess.run([sys.executable, str(script)], cwd=root, text=True, capture_output=True, timeout=10)
    assert one.returncode == 0, one.stderr + one.stdout
    rss = (root / "src/trading_team/data/news_rss.py").read_text()
    js = (root / "src/trading_team/web/static/app.js").read_text()
    assert "TradingNewsPressProvider" in rss
    assert 'statuses["tradingnews_press"] = tradingnews_status' in rss
    assert "tradingUrgencyBadge" in js
    assert "4.29.0" in (root / "src/trading_team/web/app.py").read_text()
    assert "TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED=0" in (root / ".env.example").read_text()
    two = subprocess.run([sys.executable, str(script)], cwd=root, text=True, capture_output=True, timeout=10)
    assert two.returncode == 0, two.stderr + two.stdout
    assert (root / "src/trading_team/web/static/app.js").read_text().count("const tradingUrgencyBadge") == 1


def test_installer_aborts_without_mutating_unsupported_project(tmp_path):
    root = tmp_path / "unsupported"
    (root / "src/trading_team/data").mkdir(parents=True)
    shutil.copy2(PATCH_ROOT / "apply_tradingnews_press_v429.py", root)
    shutil.copy2(PATCH_ROOT / "src/trading_team/data/tradingnews_press.py", root / "src/trading_team/data")
    (root / "src/trading_team/data/news_rss.py").write_text("class DifferentProvider: pass\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(root / "apply_tradingnews_press_v429.py")], cwd=root, text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert (root / "src/trading_team/data/news_rss.py").read_text() == "class DifferentProvider: pass\n"
