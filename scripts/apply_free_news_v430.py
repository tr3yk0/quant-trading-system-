from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import sys

VERSION_OLD = "4.29.0"
VERSION_NEW = "4.30.0"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} anchor, found {count}. No partial patch has been written.")
    return text.replace(old, new, 1)


def _append_env(text: str) -> str:
    if "FREE_NEWS_RSS_ENABLED=" in text:
        return text
    return text + '''\n\n# v4.30 optional free-news/community feeds. RSS is enabled by default; disable with 0.\nFREE_NEWS_RSS_ENABLED=1\nFREE_NEWS_MAX_ITEMS=80\nFREE_NEWS_TIMEOUT_SECONDS=7\n# Optional: add symbol-specific Yahoo Finance RSS feeds; e.g. AAPL,MSFT,BTC-USD,GC=F\nFREE_NEWS_TICKERS=\n# Optional custom RSS feeds in name=https://...;name2=https://... form\nFREE_NEWS_RSS_URLS=\n\n# Optional Reddit Data API/PRAW integration. Requires credentials and explicit Reddit access approval.\nREDDIT_ENABLED=0\nREDDIT_DATA_API_APPROVED=0\nREDDIT_CLIENT_ID=\nREDDIT_CLIENT_SECRET=\nREDDIT_USER_AGENT=AITradingTeamNews/4.30 (approved read-only headline monitor)\nREDDIT_SUBREDDITS=investing,stockmarket,algotrading\nREDDIT_POST_LIMIT=25\n\n# Optional StockTwits public JSON endpoint. Its availability/terms may change; disabled by default.\nSTOCKTWITS_ENABLED=0\nSTOCKTWITS_ACCESS_CONFIRMED=0\nSTOCKTWITS_SYMBOLS=\n\n# Local sentiment: VADER, TextBlob, or a transparent lexical fallback.\nLOCAL_NEWS_SENTIMENT_ENABLED=1\n# Optional local Ollama classification (loopback URL only; disabled by default).\nOLLAMA_NEWS_SENTIMENT_ENABLED=0\nOLLAMA_BASE_URL=http://127.0.0.1:11434\nOLLAMA_NEWS_SENTIMENT_MODEL=qwen2.5:3b\nOLLAMA_NEWS_SENTIMENT_TIMEOUT=4\nOLLAMA_NEWS_SENTIMENT_MAX_PER_CYCLE=8\n'''


def main() -> int:
    root = Path(__file__).resolve().parent
    news_path = root / "src/trading_team/data/news_rss.py"
    app_path = root / "src/trading_team/web/app.py"
    js_path = root / "src/trading_team/web/static/app.js"
    html_path = root / "src/trading_team/web/static/index.html"
    css_path = root / "src/trading_team/web/static/app.css"
    project_path = root / "pyproject.toml"
    env_example = root / ".env.example"
    readme_path = root / "README.md"
    free_adapter_path = root / "src/trading_team/data/free_news_sources.py"

    if not free_adapter_path.exists():
        print("ERROR: free_news_sources.py is missing. Extract the v4.30 patch ZIP into the project root first.", file=sys.stderr)
        return 2

    try:
        news = news_path.read_text(encoding="utf-8")
        if "from trading_team.data.tradingnews_press import TradingNewsPressProvider" not in news or "self.tradingnews_press = TradingNewsProvider()" in news:
            raise RuntimeError("news_rss.py does not contain the expected v4.29 integration; apply the combined patch driver or use the matching version")
        if "from trading_team.data.free_news_sources import FreeNewsSources" not in news:
            news = replace_once(news, "from typing import Any\n",
                                "from typing import Any\nfrom trading_team.data.free_news_sources import FreeNewsSources\n",
                                "free-news import")
        if "self.free_news_sources = FreeNewsSources()" not in news:
            news = replace_once(news, "        self.tradingnews_press = TradingNewsPressProvider()\n",
                                "        self.tradingnews_press = TradingNewsPressProvider()\n        self.free_news_sources = FreeNewsSources()\n",
                                "free-news provider init")
        status_anchor = '            "tradingnews_press": self.last_status.get("tradingnews_press") or self.tradingnews_press.status(),\n'
        if '"free_news_sources":' not in news:
            news = replace_once(news, status_anchor,
                status_anchor + '            "free_news_sources_enabled": self.free_news_sources.enabled,\n'
                '            "free_news_sources": self.last_status.get("free_news_sources") or self.free_news_sources.status(),\n',
                "free-news status")
        provider_count_old = "return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled) + int(self.tradingnews_press.enabled)"
        if "int(self.free_news_sources.enabled)" not in news:
            news = replace_once(news, provider_count_old,
                "return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled) + int(self.tradingnews_press.enabled) + int(self.free_news_sources.enabled)",
                "configured free-news provider count")
        empty_guard_old = "if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled and not self.tradingnews_press.enabled:"
        if "and not self.free_news_sources.enabled" not in news:
            news = replace_once(news, empty_guard_old,
                "if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled and not self.tradingnews_press.enabled and not self.free_news_sources.enabled:",
                "free-news empty-provider guard")
        if 'statuses["free_news_sources"] = free_news_status' not in news:
            marker = "        # De-duplicate by canonical URL; fall back to normalized headline for URL variants.\n"
            insertion = (
                "        # Optional free RSS/community sources; failures are isolated from paid and existing sources.\n"
                "        try:\n"
                "            free_news_rows, free_news_status = self.free_news_sources.fetch()\n"
                "            items.extend(free_news_rows)\n"
                "            statuses[\"free_news_sources\"] = free_news_status\n"
                "        except Exception as free_news_exc:\n"
                "            statuses[\"free_news_sources\"] = {\"status\": \"error\", \"count\": 0, \"error\": type(free_news_exc).__name__}\n"
                + marker
            )
            news = replace_once(news, marker, insertion, "news aggregation insertion")

        app = app_path.read_text(encoding="utf-8")
        if VERSION_OLD not in app and VERSION_NEW not in app:
            raise RuntimeError("web/app.py does not look like v4.29.0 source; refusing to patch it automatically")
        app = app.replace(f'VERSION = "{VERSION_OLD}"', f'VERSION = "{VERSION_NEW}"')
        app_status_anchor = '"tradingnews_press": RUNTIME.news_provider.tradingnews_press.status(),'
        if '"free_news_sources": RUNTIME.news_provider.free_news_sources.status(),' not in app:
            app = replace_once(app, app_status_anchor,
                               app_status_anchor + ' "free_news_sources": RUNTIME.news_provider.free_news_sources.status(),',
                               "free-news API diagnostics")

        js = js_path.read_text(encoding="utf-8")
        if VERSION_OLD not in js and VERSION_NEW not in js:
            raise RuntimeError("app.js does not look like v4.29.0 source; refusing to patch it automatically")
        js = js.replace(VERSION_OLD, VERSION_NEW)
        if "Free RSS" not in js:
            lines = js.splitlines(keepends=True)
            found = False
            for index, line in enumerate(lines):
                if "setTxt('newsSourceDetail'" in line and "TradingNews" in line and "`);" in line:
                    fragment = " · ${NewsHubState.status?.free_news_sources?.rss_enabled?'Free RSS on':'Free RSS off'} · ${NewsHubState.status?.free_news_sources?.reddit_enabled?'Reddit configured':'Reddit off'} · ${NewsHubState.status?.free_news_sources?.stocktwits_enabled?'StockTwits configured':'StockTwits off'}"
                    lines[index] = line.replace("`);", fragment + "`);", 1)
                    found = True
                    break
            if not found:
                raise RuntimeError("Could not find the News Hub provider-detail line in app.js")
            js = "".join(lines)
        # Expose local/source sentiment with a small neutral-strength label where the card renderer has tags.
        if "const localSentimentBadge" not in js:
            tag_marker = '    const cats=(item.categories||[]).slice(0,3).map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join(\'\');\n'
            if tag_marker in js:
                js = replace_once(js, tag_marker, tag_marker +
                    '    const localSentimentBadge=item.local_sentiment&&item.local_sentiment_score!==undefined?`<span class="news-tag sentiment-${esc(item.local_sentiment)}">Local tone ${esc(String(item.local_sentiment).toUpperCase())}</span>`:\'\';\n',
                    "local sentiment badge")
                footer_marker = "${cats}${tradingUrgencyBadge}<span class=\"news-provider-tag\">"
                if footer_marker in js:
                    js = js.replace(footer_marker, "${cats}${tradingUrgencyBadge}${localSentimentBadge}<span class=\"news-provider-tag\">", 1)
        
        html = html_path.read_text(encoding="utf-8")
        if VERSION_OLD not in html and VERSION_NEW not in html:
            raise RuntimeError("index.html does not look like v4.29.0 source; refusing to patch it automatically")
        html = html.replace(f"v={VERSION_OLD}", f"v={VERSION_NEW}").replace(f"v{VERSION_OLD}</span>", f"v{VERSION_NEW}</span>")

        css = css_path.read_text(encoding="utf-8").replace(f"v={VERSION_OLD}", f"v={VERSION_NEW}")
        if ".news-tag.sentiment-positive" not in css:
            css += "\n/* v4.30 local, non-trading sentiment context badges */\n.news-tag.sentiment-positive{border-color:rgba(34,197,94,.45);color:#16a34a}.news-tag.sentiment-negative{border-color:rgba(239,68,68,.45);color:#dc2626}.news-tag.sentiment-neutral{border-color:rgba(148,163,184,.35);color:#64748b}\n"

        project = project_path.read_text(encoding="utf-8")
        if VERSION_OLD not in project and VERSION_NEW not in project:
            raise RuntimeError("pyproject.toml does not look like v4.29.0 source; refusing to patch it automatically")
        project = project.replace(VERSION_OLD, VERSION_NEW)
        if 'news = ["praw' not in project:
            match = re.search(r'(?m)^(ml\s*=\s*\[[^\n]*\])$', project)
            if match:
                project = project[:match.end()] + '\nnews = ["praw>=7.7", "vaderSentiment>=3.3.2", "textblob>=0.18"]' + project[match.end():]

        changed: dict[Path, str] = {
            news_path: news,
            app_path: app,
            js_path: js,
            html_path: html,
            css_path: css,
            project_path: project,
        }
        start_path = root / "START.bat"
        if start_path.exists():
            changed[start_path] = start_path.read_text(encoding="utf-8", errors="replace").replace(VERSION_OLD, VERSION_NEW)
        env_text = env_example.read_text(encoding="utf-8") if env_example.exists() else ""
        new_env_text = _append_env(env_text)
        if env_text != new_env_text:
            changed[env_example] = new_env_text
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        readme = readme.replace(f"# AI Trading Team v{VERSION_OLD}", f"# AI Trading Team v{VERSION_NEW}", 1)
        if "## v4.30.0 — Free news sources and local sentiment" not in readme:
            readme += '''\n\n## v4.30.0 — Free news sources and local sentiment\n\n- Adds free financial RSS feeds (CNBC, MarketWatch, Bloomberg Markets, and Reuters/market searches via Google News RSS) with bounded headline/description ingestion; no publisher-page scraping. Optional Yahoo Finance symbol RSS is configured with `FREE_NEWS_TICKERS`.\n- Adds optional Reddit Data API via PRAW, gated by credentials plus `REDDIT_DATA_API_APPROVED=1`. Reddit policy requires API access approval; commercial uses require explicit written permission. Do not bypass platform access controls.\n- Adds an opt-in StockTwits public JSON adapter. Its developer portal currently says new API registrations are paused during a review, so the adapter is best-effort and may return access-denied/unavailable status.\n- Adds local VADER/TextBlob sentiment when installed, with a transparent lexical fallback. Optional Ollama inference is disabled by default and restricted to loopback hosts. Sentiment is contextual evidence, not a standalone trading signal.\n- Optional dependencies: `pip install -e ".[news]"`. Core RSS and lexical fallback use Python standard library.\n- Provider guide: `FREE_NEWS_SOURCES_v4.30.md`.\n'''
        changed[readme_path] = readme
        for rel in ("QUICKSTART_WINDOWS.txt",):
            path = root / rel
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if VERSION_OLD in text:
                    changed[path] = text.replace(VERSION_OLD, VERSION_NEW)

        # Validate all expected anchors above before writing. Back up first, then apply the replacements.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = root / ".runtime" / "patch_backups" / f"free_news_v430_{stamp}"
        for path, new_text in changed.items():
            if path.exists() and path.read_text(encoding="utf-8", errors="replace") != new_text:
                backup = backup_root / path.relative_to(root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
        for path, new_text in changed.items():
            if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != new_text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_text, encoding="utf-8")

        print("Free RSS/community news and local sentiment integration applied. Version metadata updated to v4.30.0.")
        print("Backups (when needed):", backup_root)
        print("RSS is enabled by default. Reddit and StockTwits stay permission-gated; Ollama stays off by default.")
        print('Optional dependencies: pip install -e ".[news]"')
        return 0
    except Exception as exc:
        print(f"PATCH ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("No patched application files were written. Run the combined installer from a v4.28.0 or v4.29.0 project root.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
