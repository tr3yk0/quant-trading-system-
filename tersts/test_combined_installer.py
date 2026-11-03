from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("test_combined_installer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_v428(root: Path, bundle: Path):
    (root / "src/trading_team/data").mkdir(parents=True)
    (root / "src/trading_team/web/static").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    for filename in ("APPLY_NEWS_PROVIDERS_v4.30.py", "apply_tradingnews_press_v429.py", "apply_free_news_v430.py"):
        shutil.copy2(bundle / filename, root / filename)
    shutil.copy2(bundle / "src/trading_team/data/tradingnews_press.py", root / "src/trading_team/data/tradingnews_press.py")
    shutil.copy2(bundle / "src/trading_team/data/free_news_sources.py", root / "src/trading_team/data/free_news_sources.py")
    (root / "src/trading_team/data/news_rss.py").write_text('''from typing import Any\nclass News:\n    def __init__(self):\n        self._load_newsapi_state()\n    def status(self):\n        return {\n            "newsapi_mode": self.newsapi_mode,\n        }\n    def provider_count(self):\n        return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled)\n    def fetch(self):\n        if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled:\n            return []\n        # De-duplicate by canonical URL; fall back to normalized headline for URL variants.\n        return []\n''', encoding="utf-8")
    (root / "src/trading_team/web/app.py").write_text('''VERSION = "4.28.0"\nSTATUS = {"newsapi_mode": getattr(RUNTIME.news_provider, "newsapi_mode", "everything"),}\n''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.js").write_text(r'''const VERSION = "4.28.0";
setTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.gdelt_enabled?'GDELT on':'GDELT off'} · ${NewsHubState.status?.newsapi_enabled?'NewsAPI on':NewsHubState.status?.newsapi_key_configured?'NewsAPI disabled':'NewsAPI key not set'}`);
    const cats=(item.categories||[]).slice(0,3).map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join('');
return `${cats}<span class="news-provider-tag">`;
''', encoding="utf-8")
    (root / "src/trading_team/web/static/index.html").write_text('''<span>v4.28.0</span><script src="app.js?v=4.28.0"></script>''', encoding="utf-8")
    (root / "src/trading_team/web/static/app.css").write_text('''.app{color:#000} /* v=4.28.0 */''', encoding="utf-8")
    (root / "pyproject.toml").write_text('''[project]\nversion = "4.28.0"\n[project.optional-dependencies]\ndev = ["pytest>=8"]\nml = ["numpy>=1.24", "scikit-learn>=1.3"]\n''', encoding="utf-8")
    (root / ".env.example").write_text("NEWS_RSS_URLS=\n", encoding="utf-8")
    (root / "README.md").write_text("# AI Trading Team v4.28.0\n", encoding="utf-8")


def test_combined_driver_runs_428_through_430(tmp_path):
    bundle = Path(__file__).resolve().parents[1]
    root = tmp_path / "project"
    root.mkdir()
    _make_v428(root, bundle)
    driver = _load(root / "APPLY_NEWS_PROVIDERS_v4.30.py")
    assert driver.main() == 0
    assert 'VERSION = "4.30.0"' in (root / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    news = (root / "src/trading_team/data/news_rss.py").read_text(encoding="utf-8")
    assert "TradingNewsPressProvider" in news
    assert "FreeNewsSources" in news
    assert "statuses[\"free_news_sources\"]" in news
    assert "FREE_NEWS_RSS_ENABLED=1" in (root / ".env.example").read_text(encoding="utf-8")
