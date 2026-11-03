from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("test_apply_free_news", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _fixture_project(root: Path, installer_source: Path):
    (root / "src/trading_team/data").mkdir(parents=True)
    (root / "src/trading_team/web/static").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    shutil.copy2(installer_source, root / "apply_free_news_v430.py")
    shutil.copy2(installer_source.parent / "src/trading_team/data/free_news_sources.py", root / "src/trading_team/data/free_news_sources.py")
    (root / "src/trading_team/data/news_rss.py").write_text('''from typing import Any\nfrom trading_team.data.tradingnews_press import TradingNewsPressProvider\nclass News:\n    def __init__(self):\n        self._load_newsapi_state()\n        self.tradingnews_press = TradingNewsPressProvider()\n    def status(self):\n        return {\n            "newsapi_mode": self.newsapi_mode,\n            "tradingnews_press": self.last_status.get("tradingnews_press") or self.tradingnews_press.status(),\n        }\n    def provider_count(self):\n        return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled) + int(self.tradingnews_press.enabled)\n    def fetch(self):\n        if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled and not self.tradingnews_press.enabled:\n            return []\n        # De-duplicate by canonical URL; fall back to normalized headline for URL variants.\n        return []\n''', encoding="utf-8")
    (root / "src/trading_team/web/app.py").write_text('''VERSION = "4.29.0"\nSTATUS = {"tradingnews_press": RUNTIME.news_provider.tradingnews_press.status(),}\n''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.js").write_text('''const VERSION = "4.29.0";\nsetTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.tradingnews_press_enabled?'TradingNews on':'TradingNews off'}`);\nconst cats=(item.categories||[]).slice(0,3).map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join('');\nconst tradingUrgencyBadge='';\nreturn `${cats}${tradingUrgencyBadge}<span class="news-provider-tag">`;\n''', encoding="utf-8")
    (root / "src/trading_team/web/static/index.html").write_text('''<span>v4.29.0</span><script src="app.js?v=4.29.0"></script>''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.css").write_text('''.app{color:#000} /* v=4.29.0 */''', encoding="utf-8")
    (root / "pyproject.toml").write_text('''[project]\nversion = "4.29.0"\n[project.optional-dependencies]\ndev = ["pytest>=8"]\nml = ["numpy>=1.24", "scikit-learn>=1.3"]\n''', encoding="utf-8")
    (root / ".env.example").write_text("NEWS_RSS_URLS=\n", encoding="utf-8")
    (root / "README.md").write_text("# AI Trading Team v4.29.0\n", encoding="utf-8")


def test_installer_wires_free_news_sources_and_bumps_version(tmp_path):
    source_dir = Path(__file__).resolve().parents[1]
    root = tmp_path / "project"
    root.mkdir()
    _fixture_project(root, source_dir / "apply_free_news_v430.py")
    module = _load(root / "apply_free_news_v430.py")
    assert module.main() == 0
    news = (root / "src/trading_team/data/news_rss.py").read_text(encoding="utf-8")
    app = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    js = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    env = (root / ".env.example").read_text(encoding="utf-8")
    assert "from trading_team.data.free_news_sources import FreeNewsSources" in news
    assert "self.free_news_sources = FreeNewsSources()" in news
    assert 'statuses["free_news_sources"] = free_news_status' in news
    assert 'VERSION = "4.30.0"' in app
    assert '"free_news_sources": RUNTIME.news_provider.free_news_sources.status()' in app
    assert "Free RSS" in js
    assert "4.30.0" in project
    assert 'news = ["praw' in project
    assert "REDDIT_DATA_API_APPROVED=0" in env


def test_installer_aborts_on_unexpected_news_anchor_without_partial_write(tmp_path):
    source_dir = Path(__file__).resolve().parents[1]
    root = tmp_path / "project"
    root.mkdir()
    _fixture_project(root, source_dir / "apply_free_news_v430.py")
    news_file = root / "src/trading_team/data/news_rss.py"
    news_file.write_text("unexpected source layout", encoding="utf-8")
    before_app = (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    module = _load(root / "apply_free_news_v430.py")
    assert module.main() == 2
    assert (root / "src/trading_team/web/app.py").read_text(encoding="utf-8") == before_app
    assert news_file.read_text(encoding="utf-8") == "unexpected source layout"
