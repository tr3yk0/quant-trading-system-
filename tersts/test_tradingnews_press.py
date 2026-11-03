from __future__ import annotations

from datetime import datetime, timezone
import json
import time


def _response(payload, status=200):
    class Response:
        def __init__(self): self.status = status
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return json.dumps(payload).encode("utf-8")[:limit]
    return Response()


def _configure(monkeypatch, tmp_path, **extra):
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "1")
    monkeypatch.setenv("TRADINGNEWS_API_KEY", "tn_live_test_secret")
    monkeypatch.setenv("TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED", "1")
    monkeypatch.setenv("TRADINGNEWS_REFRESH_SECONDS", "300")
    monkeypatch.setenv("TRADINGNEWS_STATE_PATH", str(tmp_path / "state.json"))
    for key, value in extra.items():
        monkeypatch.setenv(key, str(value))


def test_tradingnews_normalizes_headline_urgency_sentiment_tickers_and_categories(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import trading_team.data.tradingnews_press as module
    response = {"count": 1, "articles": [{
        "id": "01KNEWSID123456789012345678", "content": "Bitcoin and Ethereum rise as macro data shifts market outlook.",
        "urgency": "breaking", "sentiment": "positive",
        "published_at": "2026-10-27T09:30:00+00:00", "received_at": "2026-10-27T09:30:00.150000+00:00",
        "tickers": [{"ticker": "BTC", "sentiment": "positive"}],
    }]}
    seen = {}
    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return _response(response)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    provider = module.TradingNewsPressProvider()
    items, status = provider.fetch()
    assert status["status"] == "ok"
    assert status["count"] == 1
    assert items[0]["provider"] == "TradingNews.press API"
    assert items[0]["urgency"] == "breaking"
    assert items[0]["urgency_rank"] == 2
    assert items[0]["sentiment"] == "positive"
    assert "crypto" in items[0]["categories"]
    assert "markets" in items[0]["categories"]
    assert items[0]["tickers"] == [{"ticker": "BTC", "sentiment": "positive"}]
    assert "tn_live_test_secret" not in seen["url"]
    assert seen["headers"].get("X-api-key") == "tn_live_test_secret"
    assert "limit=50" in seen["url"]


def test_provider_does_not_fetch_without_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "1")
    monkeypatch.delenv("TRADINGNEWS_API_KEY", raising=False)
    monkeypatch.setenv("TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED", "1")
    monkeypatch.setenv("TRADINGNEWS_STATE_PATH", str(tmp_path / "state.json"))
    import trading_team.data.tradingnews_press as module
    def fail(*args, **kwargs): raise AssertionError("No key means no HTTP request")
    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    provider = module.TradingNewsPressProvider()
    items, status = provider.fetch()
    assert items == []
    assert status["status"] == "not_configured"


def test_provider_requires_explicit_dashboard_display_permission(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "1")
    monkeypatch.setenv("TRADINGNEWS_API_KEY", "tn_live_test_secret")
    monkeypatch.setenv("TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED", "0")
    monkeypatch.setenv("TRADINGNEWS_STATE_PATH", str(tmp_path / "state.json"))
    import trading_team.data.tradingnews_press as module
    def fail(*args, **kwargs): raise AssertionError("No confirmed permission means no HTTP request")
    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    provider = module.TradingNewsPressProvider()
    items, status = provider.fetch()
    assert items == []
    assert status["status"] == "display_license_confirmation_required"


def test_provider_disable_setting_overrides_configured_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "0")
    monkeypatch.setenv("TRADINGNEWS_API_KEY", "tn_live_test_secret")
    monkeypatch.setenv("TRADINGNEWS_DISPLAY_LICENSE_CONFIRMED", "1")
    monkeypatch.setenv("TRADINGNEWS_STATE_PATH", str(tmp_path / "state.json"))
    from trading_team.data.tradingnews_press import TradingNewsPressProvider
    provider = TradingNewsPressProvider()
    items, status = provider.fetch()
    assert items == []
    assert status["status"] == "disabled"


def test_rate_limit_cooldown_is_persisted_across_restart(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import trading_team.data.tradingnews_press as module
    path = tmp_path / "state.json"
    calls = []
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda req, timeout=0: (calls.append(req.full_url) or _response({"count": 0, "articles": []})))
    one = module.TradingNewsPressProvider()
    one.fetch()
    assert len(calls) == 1
    assert path.exists()
    two = module.TradingNewsPressProvider()
    items, status = two.fetch()
    assert items == []  # article cache is intentionally memory-only across process restarts
    assert status["status"] == "cooldown"
    assert len(calls) == 1
    assert status["next_allowed_at"]


def test_error_response_does_not_leak_api_key_and_waits_cooldown(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import trading_team.data.tradingnews_press as module
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **kw: _response({"detail": "Rate limit exceeded. Try again shortly."}, status=429))
    # urllib raises HTTPError for real 429 responses; simulate the same contract.
    import urllib.error
    def error_open(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(module.urllib.request, "urlopen", error_open)
    provider = module.TradingNewsPressProvider()
    items, status = provider.fetch()
    assert items == []
    assert status["status"] == "error"
    assert status["http_status"] == 429
    assert "tn_live_test_secret" not in json.dumps(status)
    assert provider.last_attempt_at


def test_long_api_content_is_bounded_not_mirrored_as_full_article(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from trading_team.data.tradingnews_press import TradingNewsPressProvider
    provider = TradingNewsPressProvider()
    result = provider._normalize({"id": "x", "content": ("A very long first sentence that describes a major market event " * 30) + "Second paragraph full article text.", "urgency": "flash"})
    assert len(result["title"]) <= 220
    assert result["summary"] == ""
    assert result["url"] == ""
    assert result["urgency"] == "flash"


def test_invalid_article_schema_returns_error_not_fake_news(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import trading_team.data.tradingnews_press as module
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: _response({"count": 1, "data": []}))
    provider = module.TradingNewsPressProvider()
    items, status = provider.fetch()
    assert items == []
    assert status["status"] == "error"
    assert "unexpected response shape" in status["error"]


def test_cached_items_are_preserved_when_manual_refresh_hits_cooldown(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import trading_team.data.tradingnews_press as module
    article = {"count": 1, "articles": [{"id": "x1", "content": "Bitcoin market headline", "urgency": "flash", "published_at": "2026-10-27T10:00:00Z"}]}
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: _response(article))
    provider = module.TradingNewsPressProvider()
    first, status = provider.fetch()
    assert len(first) == 1
    second, status = provider.fetch()
    assert len(second) == 1
    assert status["status"] == "cooldown"
    assert status["returned_cached"] is True
