from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

VERSION_OLD = "4.28.0"
VERSION_NEW = "4.29.0"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} anchor, found {count}. No partial patch has been written.")
    return text.replace(old, new, 1)


def update_text(path: Path, transform) -> bool:
    if not path.exists():
        raise FileNotFoundError(f"Required project file not found: {path}")
    old = path.read_text(encoding="utf-8")
    new = transform(old)
    if old == new:
        return False
    return True


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
    adapter_path = root / "src/trading_team/data/tradingnews_press.py"

    if not adapter_path.exists():
        print("ERROR: TradingNewsPressProvider module is missing. Extract the v4.29 patch ZIP into the v4.28 project root first.", file=sys.stderr)
        return 2

    try:
        news = news_path.read_text(encoding="utf-8")
        # Import and construct the provider after NewsAPI's persisted state is loaded.
        if "from trading_team.data.tradingnews_press import TradingNewsPressProvider" not in news:
            news = replace_once(news, "from typing import Any\n", "from typing import Any\nfrom trading_team.data.tradingnews_press import TradingNewsPressProvider\n", "news provider import")
        if "self.tradingnews_press = TradingNewsPressProvider()" not in news:
            news = replace_once(news, "        self._load_newsapi_state()\n", "        self._load_newsapi_state()\n        self.tradingnews_press = TradingNewsPressProvider()\n", "NewsAPI state init")
        if '"tradingnews_press":' not in news:
            news = replace_once(news, '            "newsapi_mode": self.newsapi_mode,\n',
                '            "newsapi_mode": self.newsapi_mode,\n'
                '            "tradingnews_press_enabled": self.tradingnews_press.enabled,\n'
                '            "tradingnews_press": self.last_status.get("tradingnews_press") or self.tradingnews_press.status(),\n',
                "news provider status")
        if "int(self.tradingnews_press.enabled)" not in news:
            news = replace_once(news, 'return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled)',
                'return len(self.urls) + int(self.gdelt_enabled) + int(self.newsapi_enabled) + int(self.tradingnews_press.enabled)',
                "configured provider count")
        if "and not self.tradingnews_press.enabled" not in news:
            news = replace_once(news, 'if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled:',
                'if not self.urls and not self.gdelt_enabled and not self.newsapi_enabled and not self.tradingnews_press.enabled:',
                "single-flight empty-provider guard")
        if 'statuses["tradingnews_press"] = tradingnews_status' not in news:
            marker = "        # De-duplicate by canonical URL; fall back to normalized headline for URL variants.\n"
            insertion = (
                '        # Optional TradingNews.press poll executes only inside this background news worker.\n'
                '        try:\n'
                '            tradingnews_rows, tradingnews_status = self.tradingnews_press.fetch()\n'
                '            items.extend(tradingnews_rows)\n'
                '            statuses["tradingnews_press"] = tradingnews_status\n'
                '        except Exception as tradingnews_exc:\n'
                '            statuses["tradingnews_press"] = {"status": "error", "count": 0, "latency_ms": None,\n'
                '                "error": f"{type(tradingnews_exc).__name__}: {str(tradingnews_exc)[:180]}", "provider": "TradingNews.press"}\n'
                + marker
            )
            news = replace_once(news, marker, insertion, "news aggregation insertion")
        # Back up before writing any modified application files.
        changed = {
            news_path: news,
        }

        # Update the app version and diagnostics in a way that fails safely if this is not v4.28.
        app = app_path.read_text(encoding="utf-8")
        if VERSION_OLD not in app and VERSION_NEW not in app:
            raise RuntimeError("web/app.py does not look like the expected v4.28.0 source; refusing to patch it automatically")
        app = app.replace(f'VERSION = "{VERSION_OLD}"', f'VERSION = "{VERSION_NEW}"')
        status_marker = '"newsapi_mode": getattr(RUNTIME.news_provider, "newsapi_mode", "everything"),'
        if "tradingnews_press" not in app:
            app = replace_once(app, status_marker, status_marker + ' "tradingnews_press": RUNTIME.news_provider.tradingnews_press.status(),', "API news provider diagnostics")
        changed[app_path] = app

        js = js_path.read_text(encoding="utf-8")
        if VERSION_OLD not in js and VERSION_NEW not in js:
            raise RuntimeError("app.js does not look like the expected v4.28.0 asset; refusing to patch it automatically")
        js = js.replace(VERSION_OLD, VERSION_NEW)
        detail_old = "setTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.gdelt_enabled?'GDELT on':'GDELT off'} · ${NewsHubState.status?.newsapi_enabled?'NewsAPI on':NewsHubState.status?.newsapi_key_configured?'NewsAPI disabled':'NewsAPI key not set'}`);"
        if "TradingNews" not in js:
            detail_new = "setTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.gdelt_enabled?'GDELT on':'GDELT off'} · ${NewsHubState.status?.newsapi_enabled?'NewsAPI on':NewsHubState.status?.newsapi_key_configured?'NewsAPI disabled':'NewsAPI key not set'} · ${NewsHubState.status?.tradingnews_press_enabled?'TradingNews on':NewsHubState.status?.tradingnews_press?.status==='display_license_confirmation_required'?'TradingNews permission needed':NewsHubState.status?.tradingnews_press?.status==='not_configured'?'TradingNews key not set':'TradingNews off'}`);"
            js = replace_once(js, detail_old, detail_new, "News Hub provider status line")
        # Show TradingNews urgency as a visible tag on a card.
        if "const tradingUrgencyBadge" not in js:
            card_marker = "    const cats=(item.categories||[]).slice(0,3).map(c=>`<span class=\"news-tag\">${esc(_newsCategoryLabel(c))}</span>`).join('');\n"
            js = replace_once(js, card_marker, card_marker +
                '    const tradingUrgencyBadge=item.provider===\'TradingNews.press API\'&&item.urgency?`<span class="news-tag urgency-${esc(item.urgency)}">${esc(String(item.urgency).toUpperCase())}</span>`:\'\';\n',
                "headline category badge insertion")
            footer_marker = '${cats}<span class="news-provider-tag">'
            js = replace_once(js, footer_marker, '${cats}${tradingUrgencyBadge}<span class="news-provider-tag">', "headline urgency tag")
        changed[js_path] = js

        html = html_path.read_text(encoding="utf-8")
        if VERSION_OLD not in html and VERSION_NEW not in html:
            raise RuntimeError("index.html does not look like the expected v4.28.0 asset; refusing to patch it automatically")
        html = html.replace(f"v={VERSION_OLD}", f"v={VERSION_NEW}").replace(f"v{VERSION_OLD}</span>", f"v{VERSION_NEW}</span>")
        changed[html_path] = html

        css = css_path.read_text(encoding="utf-8")
        css = css.replace(f"v={VERSION_OLD}", f"v={VERSION_NEW}")
        if ".news-tag.urgency-breaking" not in css:
            css += '\n/* TradingNews.press urgency markers */\n.news-tag.urgency-breaking{background:rgba(239,68,68,.17);border-color:rgba(239,68,68,.55);color:#ef4444;font-weight:800}.news-tag.urgency-flash{background:rgba(245,158,11,.16);border-color:rgba(245,158,11,.5);color:#d97706;font-weight:800}\n'
        changed[css_path] = css

        project = project_path.read_text(encoding="utf-8")
        if VERSION_OLD not in project and VERSION_NEW not in project:
            raise RuntimeError("pyproject.toml does not look like the expected v4.28.0 package; refusing to patch it automatically")
        project = project.replace(VERSION_OLD, VERSION_NEW)
        changed[project_path] = project

        start_path = root / "START.bat"
        if start_path.exists():
            start = start_path.read_text(encoding="utf-8", errors="replace")
            changed[start_path] = start.replace(VERSION_OLD, VERSION_NEW)

        env_text = env_example.read_text(encoding="utf-8") if env_example.exists() else ""
        if "TRADINGNEWS_API_KEY=" not in env_text:
            env_text += '''\n\n# Optional TradingNews.press market-news API (commercial provider; disabled until key/permission are set).\n# REST polling is used by this integration; minimum local interval is 60 seconds, default 300s.\nTRADINGNEWS_ENABLED=1\nTRADINGNEWS_API_KEY=\nTRADINGNEWS_DISPLAY_LICENSE_CONFIRMED=0\nTRADINGNEWS_REFRESH_SECONDS=300\nTRADINGNEWS_PAGE_SIZE=50\n# Optional isolated state path for the provider cooldown.\n# TRADINGNEWS_STATE_PATH=.runtime/tradingnews_press_state.json\n'''
            changed[env_example] = env_text

        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        readme = readme.replace(f"# AI Trading Team v{VERSION_OLD}", f"# AI Trading Team v{VERSION_NEW}", 1)
        if "## v4.29.0 — TradingNews.press" not in readme:
            section = '''\n## v4.29.0 — Optional TradingNews.press provider\n\n- Adds TradingNews.press `/v1/news` REST polling to the existing News Hub alongside RSS/Atom, GDELT and NewsAPI. Headlines include urgency, sentiment and tickers where the plan supplies them.\n- API key is sent only in the `X-API-Key` header. Last-attempt time persists in `.runtime/tradingnews_press_state.json`, with a default 300-second interval and a hard minimum of 60 seconds.\n- Provider remains inactive without a key and `TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED=1`. TradingNews terms prohibit redistribution of raw API data without a redistribution license; confirm that your intended dashboard use is permitted before enabling display to other users.\n- TradingNews is optional; unavailable keys, permission acknowledgement, rate limits and service errors are surfaced per provider and do not disable RSS/GDELT/NewsAPI.\n- Integration guide: `TRADINGNEWS_PRESS_v4.29.md`.\n'''
            readme = readme + section
            changed[readme_path] = readme

        for test_rel in ("tests/test_optimizations.py", "tests/test_quant_navigation.py"):
            test_path = root / test_rel
            if test_path.exists():
                test_text = test_path.read_text(encoding="utf-8")
                if VERSION_OLD in test_text:
                    changed[test_path] = test_text.replace(VERSION_OLD, VERSION_NEW)
        quickstart = root / "QUICKSTART_WINDOWS.txt"
        if quickstart.exists():
            qs_text = quickstart.read_text(encoding="utf-8", errors="replace")
            if VERSION_OLD in qs_text:
                changed[quickstart] = qs_text.replace(VERSION_OLD, VERSION_NEW)

        # Write backups and modified files only after all markers have been validated.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = root / ".runtime" / "patch_backups" / f"tradingnews_v429_{stamp}"
        for path, new_text in changed.items():
            if not path.exists() or path.read_text(encoding="utf-8", errors="replace") == new_text:
                continue
            rel = path.relative_to(root)
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        for path, new_text in changed.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
        print("TradingNews.press integration applied. Version metadata updated to v4.29.0.")
        print("Backups (when needed):", backup_root)
        print("Next: configure TRADINGNEWS_API_KEY in your existing .env; only set TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED=1 if your license permits the intended display.")
        print("Run: python -m compileall -q src && pytest -q tests/test_tradingnews_press.py tests/test_news_hub_v426.py")
        return 0
    except Exception as exc:
        print(f"PATCH ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("No patched application files were written. Check that this is the v4.28.0 project root and use the patch matching your project version.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
