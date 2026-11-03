"""Project-wide pytest options, registered before nested toolkit conftests are loaded."""

def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="enable tests that contact live data providers")


import pytest


@pytest.fixture(autouse=True)
def disable_live_optional_news_providers_by_default(monkeypatch):
    """Keep unrelated unit tests deterministic; provider-specific tests opt in explicitly."""
    monkeypatch.setenv("FREE_NEWS_RSS_ENABLED", "0")
    monkeypatch.setenv("REDDIT_ENABLED", "0")
    monkeypatch.setenv("STOCKTWITS_ENABLED", "0")
    monkeypatch.setenv("TRADINGNEWS_ENABLED", "0")
    monkeypatch.setenv("OLLAMA_NEWS_SENTIMENT_ENABLED", "0")
