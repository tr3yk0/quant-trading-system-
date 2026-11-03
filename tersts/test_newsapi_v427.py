from __future__ import annotations

import json
import time


def _response(payload):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1):
            raw = json.dumps(payload).encode("utf-8")
            return raw[:limit]
    return Response()


def test_newsapi_everything_maps_articles_and_keeps_key_out_of_url(monkeypatch):
    import trading_team.data.news_rss as module
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-secret-key")
    monkeypatch.setenv("NEWSAPI_MODE", "everything")
    monkeypatch.setenv("NEWSAPI_PAGE_SIZE", "25")
    seen = {}
    payload = {"status": "ok", "totalResults": 1, "articles": [{
        "source": {"id": "financial-times", "name": "Financial Times"},
        "author": "Reporter", "title": "Central bank keeps policy rate unchanged",
        "description": "A short publisher-provided summary.",
        "url": "https://publisher.example/news/rates",
        "publishedAt": "2026-10-26T10:30:00Z", "content": "Truncated text",
    }]}
    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return _response(payload)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    provider = module.RSSNewsProvider(urls={}, timeout=3)
    rows, status = provider._fetch_newsapi()
    assert status["status"] == "ok"
    assert status["count"] == 1
    assert rows[0]["provider"] == "NewsAPI"
    assert rows[0]["source"] == "Financial Times"
    assert rows[0]["summary"] == "A short publisher-provided summary."
    assert rows[0]["published"] == "2026-10-26T10:30:00Z"
    assert "central_banks" in rows[0]["categories"]
    assert "test-secret-key" not in seen["url"]
    assert seen["headers"].get("X-api-key") == "test-secret-key"
    assert "pageSize=25" in seen["url"]


def test_newsapi_headlines_mode_uses_headline_endpoint(monkeypatch):
    import trading_team.data.news_rss as module
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWSAPI_API_KEY", "key-for-test")
    monkeypatch.setenv("NEWSAPI_MODE", "headlines")
    monkeypatch.setenv("NEWSAPI_COUNTRY", "gb")
    monkeypatch.setenv("NEWSAPI_CATEGORY", "business")
    seen = {}
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda req, timeout=0: (seen.update(url=req.full_url) or _response({"status": "ok", "totalResults": 0, "articles": []})))
    provider = module.RSSNewsProvider(urls={}, timeout=3)
    rows, status = provider._fetch_newsapi()
    assert rows == []
    assert status["status"] == "ok"
    assert seen["url"].startswith(module.NEWSAPI_HEADLINES_ENDPOINT)
    assert "country=gb" in seen["url"]
    assert "category=business" in seen["url"]


def test_newsapi_requires_key_and_is_optional(monkeypatch):
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    # Keep this legacy test isolated from the new optional v4.30 providers.
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.delenv("NEWSAPI_API_KEY", raising=False)
    from trading_team.data.news_rss import RSSNewsProvider
    provider = RSSNewsProvider(urls={})
    assert provider.newsapi_enabled is False
    assert provider.newsapi_key_configured is False
    assert provider.configured_count == 0
    assert provider.status()["newsapi_key_configured"] is False
    assert provider.refresh_async() is False


def test_newsapi_rate_limit_is_independent_of_rss_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWSAPI_API_KEY", "key-for-test")
    monkeypatch.setenv("NEWSAPI_REFRESH_SECONDS", "900")
    monkeypatch.setenv("NEWSAPI_STATE_PATH", str(tmp_path / "newsapi_state.json"))
    from trading_team.data.news_rss import RSSNewsProvider
    provider = RSSNewsProvider(urls={})
    provider.newsapi_last_fetch_epoch = time.time() - 30
    result = provider.fetch()
    assert result == []
    assert provider.last_status["newsapi"]["status"] == "cooldown"
    assert provider.last_status["newsapi"]["next_allowed_at"]


def test_newsapi_response_errors_are_reported_without_disabling_other_sources(monkeypatch, tmp_path):
    import trading_team.data.news_rss as module
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWSAPI_API_KEY", "key-for-test")
    monkeypatch.setenv("NEWSAPI_MODE", "everything")
    monkeypatch.setenv("NEWSAPI_STATE_PATH", str(tmp_path / "newsapi_state.json"))
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda req, timeout=0: _response({"status": "error", "code": "apiKeyInvalid", "message": "Invalid API key."}))
    provider = module.RSSNewsProvider(urls={})
    provider.fetch()
    assert provider.last_status["newsapi"]["status"] == "error"
    assert "apiKeyInvalid" in provider.last_status["newsapi"]["error"]


def test_newsapi_request_cooldown_survives_provider_restart(monkeypatch, tmp_path):
    import trading_team.data.news_rss as module
    state_path = tmp_path / "persisted-newsapi-state.json"
    monkeypatch.setenv("NEWS_RSS_ENABLED", "1")
    monkeypatch.setenv("NEWS_GDELT_ENABLED", "0")
    monkeypatch.setenv("NEWSAPI_ENABLED", "1")
    monkeypatch.setenv("NEWSAPI_API_KEY", "key-for-test")
    monkeypatch.setenv("NEWSAPI_REFRESH_SECONDS", "900")
    monkeypatch.setenv("NEWSAPI_STATE_PATH", str(state_path))
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda req, timeout=0: _response({"status": "ok", "totalResults": 0, "articles": []}))
    first = module.RSSNewsProvider(urls={})
    first.fetch()
    assert state_path.exists()
    second = module.RSSNewsProvider(urls={})
    assert second.newsapi_last_fetch_epoch > 0
    second.fetch()
    assert second.last_status["newsapi"]["status"] == "cooldown"
