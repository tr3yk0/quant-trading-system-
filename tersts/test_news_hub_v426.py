from __future__ import annotations

import io
import json
from pathlib import Path


def test_default_public_news_sources_are_enabled_and_open_data(monkeypatch):
    monkeypatch.delenv("NEWS_RSS_URLS", raising=False)
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_RSS_INCLUDE_DEFAULTS", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "1")
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "1")
    from trading_team.data.news_rss import RSSNewsProvider

    provider = RSSNewsProvider()
    assert provider.enabled is True
    assert provider.gdelt_enabled is True
    assert "google_news_business" in provider.urls
    assert "federal_reserve" in provider.urls
    assert provider.configured_count == (
        len(provider.urls) + int(provider.gdelt_enabled) + int(provider.newsapi_enabled)
        + int(provider.tradingnews_press.enabled) + int(provider.free_news_sources.enabled)
    )
    assert provider.free_news_sources.enabled is True


def test_rss_feed_parsing_keeps_headline_summary_link_and_category(monkeypatch):
    import trading_team.data.news_rss as module

    raw = b'''<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Central bank signals rate decision as inflation eases</title>
      <description><![CDATA[<p>Policy update summary &amp; outlook.</p>]]></description>
      <link>https://publisher.example/news/central-bank</link>
      <source url="https://publisher.example">Publisher News</source>
      <pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>'''

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return raw[:limit]

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    provider = module.RSSNewsProvider(urls={"test_feed": "https://publisher.example/rss.xml"}, timeout=2, max_items=20)
    rows, status = provider._fetch_rss("test_feed", "https://publisher.example/rss.xml")
    assert status["status"] == "ok"
    assert len(rows) == 1
    assert rows[0]["title"].startswith("Central bank")
    assert rows[0]["summary"] == "Policy update summary & outlook."
    assert rows[0]["url"] == "https://publisher.example/news/central-bank"
    assert rows[0]["source"] == "Publisher News"
    assert "central_banks" in rows[0]["categories"]


def test_gdelt_metadata_is_attributed_and_not_mislabeled_as_publisher_time(monkeypatch):
    import trading_team.data.news_rss as module
    payload = {"articles": [{
        "title": "Oil and gold markets react to geopolitical tension",
        "url": "https://publisher.example/markets-story",
        "domain": "publisher.example",
        "seendate": "20261026T101500Z",
        "sourcecountry": "United States",
        "language": "English",
    }]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return json.dumps(payload).encode()[:limit]
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    provider = module.RSSNewsProvider(urls={}, timeout=2, max_items=50)
    rows, status = provider._fetch_gdelt()
    assert status["status"] == "ok"
    assert rows[0]["provider"] == "GDELT DOC API"
    assert rows[0]["timestamp_kind"] == "gdelt_seen_at_not_publisher_time"
    assert rows[0]["published"] == "2026-10-26T10:15:00Z"
    assert "commodities" in rows[0]["categories"]
    assert rows[0]["source"] == "publisher.example"


def test_news_items_reject_non_https_links():
    from trading_team.data.news_rss import _normalise_item
    assert _normalise_item({"title": "bad", "url": "javascript:alert(1)"}) is None
    assert _normalise_item({"title": "bad", "url": "http://example.com"}) is None
    assert _normalise_item({"title": "good", "url": "https://example.com/a"})["url"].startswith("https://")


def _get_handler(path, runtime):
    from trading_team.web import app as web_app
    handler = web_app.Handler.__new__(web_app.Handler)
    handler.path = path
    captured = {}
    handler.send_json = lambda payload, code=200: captured.update(payload=payload, status=code)
    return handler, captured


def test_news_get_route_filters_category_and_query(monkeypatch):
    from trading_team.web import app as web_app

    class Provider:
        enabled = True
        configured_count = 2
        urls = {"sample": "https://example.com/feed.xml"}
        gdelt_enabled = True
        last_items = []
        last_status = {"sample": {"status": "ok", "count": 2}}
        last_fetch_at = "2026-10-26T10:00:00Z"
        last_fetch_epoch = 1
        last_error = None
        refreshing = False
        def status(self):
            return {"enabled": True, "configured_feeds": 2, "rss_feeds": 1, "gdelt_enabled": True,
                    "items": 2, "last_fetch_at": self.last_fetch_at, "refreshing": False,
                    "last_error": None, "feeds": self.last_status, "terms": "headline metadata only"}

    class Runtime:
        news_provider = Provider()
        news_items = [
            {"title": "Central bank changes interest rate", "summary": "Policy", "source": "Fed", "provider": "RSS", "url": "https://example.com/a", "published": "2026-10-26T10:00:00Z", "categories": ["central_banks", "macro"]},
            {"title": "Bitcoin rallies after ETF flows", "summary": "Crypto market", "source": "Wire", "provider": "GDELT", "url": "https://example.com/b", "published": "2026-10-26T09:00:00Z", "categories": ["crypto", "markets"]},
        ]

    monkeypatch.setattr(web_app, "RUNTIME", Runtime())
    handler, result = _get_handler("/api/news?category=crypto&q=bitcoin&limit=10", Runtime())
    handler.do_GET()
    assert result["status"] == 200
    assert result["payload"]["count"] == 1
    assert result["payload"]["items"][0]["title"].startswith("Bitcoin")
    assert result["payload"]["configured"] is True


def test_news_tv_and_news_page_are_connected_in_ui():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    assert 'data-page="news"' in html
    assert 'id="news"' in html
    assert 'id="newsTvPlayer"' in html
    assert 'data-news-station="aljazeera"' in html
    assert 'function selectNewsStation(id)' in js
    assert 'function refreshNewsHub()' in js
    assert "news:'News & Live TV'" in js



def test_async_refresh_is_single_flight_and_records_status(monkeypatch):
    import threading
    import time
    from trading_team.data.news_rss import RSSNewsProvider

    entered = threading.Event()
    release = threading.Event()
    provider = RSSNewsProvider(urls={"feed": "https://publisher.example/rss.xml"}, timeout=2)

    def slow_fetch():
        entered.set()
        release.wait(timeout=1.5)
        provider.last_items = [{"title": "test", "url": "https://publisher.example/a"}]
        provider.last_status = {"feed": {"status": "ok", "count": 1}}
        return provider.last_items

    monkeypatch.setattr(provider, "fetch", slow_fetch)
    assert provider.refresh_async(force=True) is True
    assert entered.wait(timeout=1)
    assert provider.refresh_async(force=True) is False
    release.set()
    deadline = time.time() + 2
    while provider.refreshing and time.time() < deadline:
        time.sleep(0.01)
    assert provider.refreshing is False
    assert provider.last_fetch_at
    assert provider.status()["items"] == 1


def test_news_refresh_post_returns_without_waiting_for_network(monkeypatch):
    import json
    from trading_team.web import app as web_app

    class Provider:
        enabled = True
        refreshing = False
        last_items = []
        last_status = {}
        gdelt_enabled = True
        configured_count = 2
        urls = {"feed": "https://example.com/feed.xml"}
        def refresh_async(self, force=False):
            self.refreshing = True
            return True
        def status(self):
            return {"enabled": True, "configured_feeds": 2, "rss_feeds": 1, "gdelt_enabled": True,
                    "items": 0, "last_fetch_at": None, "refreshing": self.refreshing,
                    "last_error": None, "feeds": {}, "terms": "headline metadata only"}

    class Runtime:
        news_provider = Provider()
        news_items = []

    monkeypatch.setattr(web_app, "RUNTIME", Runtime())
    handler = web_app.Handler.__new__(web_app.Handler)
    handler.path = "/api/news/refresh"
    handler.command = "POST"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    captured = {}
    handler.send_json = lambda payload, code=200: captured.update(payload=payload, status=code)
    handler.do_POST()
    assert captured["status"] == 202
    assert captured["payload"]["started"] is True
    assert captured["payload"]["refreshing"] is True


def test_news_refresh_disabled_returns_explicit_status(monkeypatch):
    from trading_team.web import app as web_app

    class Provider:
        enabled = False
        refreshing = False
        last_items = []
        last_status = {}
        gdelt_enabled = False
        configured_count = 0
        urls = {}
        def refresh_async(self, force=False): return False
        def status(self): return {"enabled": False}

    class Runtime:
        news_provider = Provider()
        news_items = []

    monkeypatch.setattr(web_app, "RUNTIME", Runtime())
    handler = web_app.Handler.__new__(web_app.Handler)
    handler.path = "/api/news/refresh"
    handler.command = "POST"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    captured = {}
    handler.send_json = lambda payload, code=200: captured.update(payload=payload, status=code)
    handler.do_POST()
    assert captured["status"] == 503
    assert captured["payload"]["code"] == "NEWS_RSS_DISABLED"
