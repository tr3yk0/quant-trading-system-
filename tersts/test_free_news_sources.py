from __future__ import annotations

import json
from urllib.parse import urlparse


def _response(body: bytes, status: int = 200):
    class Response:
        def __init__(self):
            self.status = status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, limit=-1):
            return body[:limit] if limit >= 0 else body
    return Response()


def _clear_news_env(monkeypatch):
    keys = [
        "FREE_NEWS_RSS_ENABLED", "FREE_NEWS_RSS_URLS", "FREE_NEWS_TICKERS", "FREE_NEWS_MAX_ITEMS",
        "FREE_NEWS_TIMEOUT_SECONDS", "REDDIT_ENABLED", "REDDIT_DATA_API_APPROVED", "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT", "REDDIT_SUBREDDITS", "REDDIT_POST_LIMIT",
        "STOCKTWITS_ENABLED", "STOCKTWITS_ACCESS_CONFIRMED", "STOCKTWITS_SYMBOLS",
        "LOCAL_NEWS_SENTIMENT_ENABLED", "OLLAMA_NEWS_SENTIMENT_ENABLED", "OLLAMA_BASE_URL",
        "OLLAMA_NEWS_SENTIMENT_MODEL", "OLLAMA_NEWS_SENTIMENT_TIMEOUT", "OLLAMA_NEWS_SENTIMENT_MAX_PER_CYCLE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_rss_free_sources_parses_headline_summary_dates_and_categories(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_URLS", "test_feed=https://example.com/news.xml")
    monkeypatch.setenv("LOCAL_NEWS_SENTIMENT_ENABLED", "1")
    import trading_team.data.free_news_sources as module
    xml = b'''<?xml version="1.0"?><rss><channel><item><title>Stocks rally on strong earnings</title><link>https://example.com/story/1</link><description><![CDATA[<p>Markets rose after a strong report.</p>]]></description><pubDate>Tue, 27 Oct 2026 10:00:00 GMT</pubDate><guid>story-1</guid></item></channel></rss>'''
    monkeypatch.setattr(module, "_safe_get", lambda url, timeout=0, max_bytes=0: xml)
    provider = module.FreeNewsSources(max_items=10)
    rows, status = provider.fetch()
    assert status["status"] == "ok"
    assert status["rss"]["feeds"]["test_feed"]["count"] == 1
    assert rows[0]["provider"] == "Free RSS"
    assert rows[0]["title"] == "Stocks rally on strong earnings"
    assert rows[0]["summary"] == "Markets rose after a strong report."
    assert rows[0]["url"] == "https://example.com/story/1"
    assert rows[0]["local_sentiment"] in {"positive", "neutral", "negative"}
    assert rows[0]["local_sentiment_method"]


def test_custom_feed_rejects_non_https_and_explicitly_disabled_rss(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("FREE_NEWS_RSS_URLS", "unsafe=http://example.com/feed.xml")
    from trading_team.data.free_news_sources import FreeNewsSources
    provider = FreeNewsSources()
    rows, status = provider.fetch()
    assert rows == []
    assert status["rss"]["status"] == "disabled"
    assert not provider.enabled


def test_reddit_requires_explicit_api_approval_before_import_or_network(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("REDDIT_ENABLED", "1")
    monkeypatch.setenv("REDDIT_DATA_API_APPROVED", "0")
    from trading_team.data.free_news_sources import FreeNewsSources
    provider = FreeNewsSources()
    rows, status = provider.fetch()
    assert rows == []
    assert status["reddit"]["status"] == "approval_required"
    assert not provider.enabled


def test_reddit_missing_credentials_stays_config_error(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("REDDIT_ENABLED", "1")
    monkeypatch.setenv("REDDIT_DATA_API_APPROVED", "1")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "")
    from trading_team.data.free_news_sources import FreeNewsSources
    rows, status = FreeNewsSources().fetch()
    assert rows == []
    assert status["reddit"]["status"] == "not_configured"


def test_stocktwits_requires_access_confirmation_and_tickers(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("STOCKTWITS_ENABLED", "1")
    monkeypatch.setenv("STOCKTWITS_ACCESS_CONFIRMED", "0")
    monkeypatch.setenv("STOCKTWITS_SYMBOLS", "AAPL")
    from trading_team.data.free_news_sources import FreeNewsSources
    rows, status = FreeNewsSources().fetch()
    assert rows == []
    assert status["stocktwits"]["status"] == "access_confirmation_required"


def test_stocktwits_parses_messages_and_native_sentiment(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("STOCKTWITS_ENABLED", "1")
    monkeypatch.setenv("STOCKTWITS_ACCESS_CONFIRMED", "1")
    monkeypatch.setenv("STOCKTWITS_SYMBOLS", "AAPL")
    import trading_team.data.free_news_sources as module
    payload = {"messages": [{
        "id": 123,
        "body": "AAPL looks bullish after earnings",
        "created_at": "2026-10-27T10:05:00Z",
        "entities": {"sentiment": {"basic": "Bullish"}},
    }]}
    monkeypatch.setattr(module, "_safe_get", lambda url, timeout=0, max_bytes=0: json.dumps(payload).encode())
    rows, status = module.FreeNewsSources().fetch()
    assert status["stocktwits"]["status"] == "ok"
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["provider_sentiment"] == "positive"
    assert rows[0]["source_sentiment"] == "positive"
    assert rows[0]["message_id"] == "123"


def test_local_sentiment_lexical_fallback_is_bounded_and_repeatable(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("LOCAL_NEWS_SENTIMENT_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_NEWS_SENTIMENT_ENABLED", "0")
    from trading_team.data.free_news_sources import LocalSentimentAnalyzer
    analyzer = LocalSentimentAnalyzer()
    analyzer._engine = "lexical_fallback"
    one = analyzer.analyze("Strong gains and bullish growth")
    two = analyzer.analyze("Strong gains and bullish growth")
    bad = analyzer.analyze("Crash and weak recession risks")
    assert one == two
    assert one["local_sentiment"] == "positive"
    assert bad["local_sentiment"] == "negative"
    assert -1 <= one["local_sentiment_score"] <= 1


def test_ollama_remote_host_is_rejected_without_network(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_NEWS_SENTIMENT_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://some-remote-host.example")
    import trading_team.data.free_news_sources as module
    def must_not_call(*args, **kwargs):
        raise AssertionError("Remote Ollama endpoint should be blocked")
    monkeypatch.setattr(module, "urlopen", must_not_call)
    analyzer = module.LocalSentimentAnalyzer()
    assert analyzer._ollama_is_loopback() is False
    result = analyzer.analyze("Stocks rally strongly")
    assert result["local_sentiment_method"] != "ollama:qwen2.5:3b"
    assert analyzer.status()["ollama_enabled"] is False


def test_yahoo_symbol_rss_added_for_configured_tickers(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_TICKERS", "AAPL,BTC-USD,invalid ticker")
    from trading_team.data.free_news_sources import FreeNewsSources
    provider = FreeNewsSources()
    assert "yahoo_AAPL" in provider.feeds
    assert "yahoo_BTCUSD" in provider.feeds
    assert all(urlparse(url).scheme == "https" for url in provider.feeds.values())


def test_provider_failure_does_not_leak_feed_url_or_credentials(monkeypatch):
    _clear_news_env(monkeypatch)
    monkeypatch.setenv("FREE_NEWS_RSS_URLS", "some_feed=https://example.com/feed")
    import trading_team.data.free_news_sources as module
    def fail(*args, **kwargs):
        raise RuntimeError("provider connection failed")
    monkeypatch.setattr(module, "_safe_get", fail)
    rows, status = module.FreeNewsSources().fetch()
    assert rows == []
    assert status["rss"]["status"] == "error"
    assert status["rss"]["feeds"]["some_feed"]["error"] == "RuntimeError"
