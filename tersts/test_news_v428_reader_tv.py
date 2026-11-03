from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_default_news_sources_include_dedicated_crypto_feeds(monkeypatch):
    monkeypatch.delenv("NEWS_RSS_URLS", raising=False)
    monkeypatch.setenv("NEWS_RSS_INCLUDE_DEFAULTS", "1")
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "1")
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    from trading_team.data.news_rss import RSSNewsProvider
    provider = RSSNewsProvider()
    assert "google_news_crypto" in provider.urls
    assert "cointelegraph_crypto" in provider.urls
    assert "coindesk_crypto" in provider.urls
    assert "theblock_crypto" in provider.urls


def test_crypto_classification_uses_source_and_crypto_aliases():
    from trading_team.data.news_rss import _normalise_item
    a = _normalise_item({"title": "Protocol update announced", "source": "CoinDesk Crypto", "url": "https://example.com/a"})
    b = _normalise_item({"title": "BTC and ETH exchange-traded products see new flows", "source": "Wire", "url": "https://example.com/b"})
    c = _normalise_item({"title": "Whether the economic decision changes", "source": "General news", "url": "https://example.com/c"})
    assert "crypto" in a["categories"]
    assert "crypto" in b["categories"]
    assert "crypto" not in c["categories"]


def test_in_app_news_story_reader_replaces_direct_article_card_links():
    html = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "src/trading_team/web/static/app.css").read_text(encoding="utf-8")
    assert 'id="newsStoryDialog"' in html
    assert 'id="newsStorySummary"' in html
    assert 'id="newsStoryRelated"' in html
    assert 'function openNewsStoryByIndex(index)' in js
    assert 'Read more in News Desk' in js
    assert 'newsStoryPublisherLink' in js
    assert 'target="_blank" rel="noopener noreferrer"' not in js[js.index('function renderNewsHeadlines'):js.index('function renderNewsFeedStatuses')]
    assert '.news-story-dialog::backdrop' in css


def test_tv_switcher_has_native_shell_and_alternate_player_mode():
    html = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "src/trading_team/web/static/app.css").read_text(encoding="utf-8")
    assert 'class="news-tv-system-chrome"' in html
    assert 'id="newsTvLocalClock"' in html
    assert 'id="newsTvModeBtn"' in html
    assert 'function toggleNewsPlayerMode()' in js
    assert 'https://news.sky.com/watch-live?varget=target' in js
    assert 'newsTvLoadTimer' in js
    assert '.news-tv-screen' in css


def test_crypto_filter_has_useful_empty_state_and_show_all_option():
    js = (ROOT / "src/trading_team/web/static/app.js").read_text(encoding="utf-8")
    assert "There are no stories in this category in the currently loaded feed window" in js
    assert 'onclick="setNewsCategory(\'all\')"' in js


def test_asset_and_application_versions_are_431():
    html = (ROOT / "src/trading_team/web/static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "src/trading_team/web/app.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'app.css?v=4.39.0' in html
    assert 'app.js?v=4.39.0' in html
    assert 'VERSION = "4.39.0"' in app
    assert 'version = "4.39.0"' in pyproject


def test_news_scheduler_runs_independently_and_stops_cleanly(monkeypatch):
    import threading
    from trading_team.data.news_rss import RSSNewsProvider
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    provider = RSSNewsProvider(urls={"sample": "https://example.com/feed.xml"})
    called = threading.Event()
    def fake_refresh(*args, **kwargs):
        called.set()
        return True
    monkeypatch.setattr(provider, "refresh_async", fake_refresh)
    assert provider.start_scheduler(interval_seconds=2, refresh_on_start=True) is True
    assert called.wait(timeout=1.0)
    provider.stop_scheduler(timeout=1.0)
    assert provider.start_scheduler(interval_seconds=2, refresh_on_start=False) is True
    provider.stop_scheduler(timeout=1.0)


def test_latest_item_cap_preserves_crypto_category_coverage(monkeypatch):
    from trading_team.data.news_rss import RSSNewsProvider, _normalise_item
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    provider = RSSNewsProvider(urls={"fixture": "https://example.com/feed.xml"}, max_items=10)
    rows = []
    for i in range(30):
        rows.append(_normalise_item({
            "title": f"Global economic markets report {i}", "source": "Wire",
            "summary": "Economic market developments",
            "published": f"2026-10-27T12:{i:02d}:00Z",
            "url": f"https://wire.example/macro-{i}",
        }))
    for i in range(5):
        rows.append(_normalise_item({
            "title": f"Digital asset crypto report {i}", "source": "CoinDesk",
            "summary": "Bitcoin and Ethereum",
            "published": f"2026-10-26T10:0{i}:00Z",
            "url": f"https://coindesk.example/crypto-{i}",
        }))
    rows = [row for row in rows if row]
    monkeypatch.setattr(provider, "_fetch_rss", lambda name, url: (rows, {"status": "ok", "count": len(rows), "latency_ms": 1, "error": None}))
    result = provider.fetch()
    assert len(result) == 10
    assert any("crypto" in row["categories"] for row in result)
