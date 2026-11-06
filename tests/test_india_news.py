"""Indian news and social sources: ET/Business Standard RSS, Indian subreddits, no StockTwits."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import india_rss, reddit


def _item(title, day, link=None):
    return {"title": title, "summary": "", "link": link or title,
            "published": format_datetime(datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=UTC))}


@pytest.fixture
def feeds(monkeypatch):
    india_rss._names.cache_clear()
    monkeypatch.setattr(india_rss, "get_company_profile", lambda t: {"longName": "Bharti Airtel Limited"})
    monkeypatch.setattr(india_rss, "cached_json", lambda namespace, key, ttl, fetch: fetch())
    served = {}
    monkeypatch.setattr(india_rss, "_fetch", lambda url: served.get(url, []))
    yield served
    india_rss._names.cache_clear()


@pytest.mark.unit
def test_headlines_naming_the_company_in_the_window_are_served_once(feeds):
    et, bs = india_rss.FEEDS["Economic Times Markets"], india_rss.FEEDS["Business Standard Markets"]
    feeds[et] = [_item("Bharti Airtel posts Q2 profit", "2026-11-05", "a"), _item("Nifty ends higher", "2026-11-05"),
                 _item("Bharti Airtel old news", "2026-10-01"), _item("BHARTIARTL hits record", "2026-11-04")]
    feeds[bs] = [_item("Bharti Airtel posts Q2 profit", "2026-11-05", "a"), _item("Old", "2026-09-01")]
    out = india_rss.get_news("BHARTIARTL.NS", "2026-11-01", "2026-11-06")
    assert out.count("Bharti Airtel posts Q2 profit") == 1
    assert "BHARTIARTL hits record" in out
    assert "Nifty ends higher" not in out and "old news" not in out


@pytest.mark.unit
def test_no_headline_lets_the_next_vendor_try(feeds):
    feeds[india_rss.FEEDS["Economic Times Markets"]] = [_item("Nifty ends higher", "2026-11-05")]
    with pytest.raises(NoMarketDataError):
        india_rss.get_news("BHARTIARTL.NS", "2026-11-01", "2026-11-06")


@pytest.mark.unit
def test_one_feed_down_does_not_hide_the_others(feeds, monkeypatch):
    def fetch(url):
        if url == india_rss.FEEDS["Economic Times Markets"]:
            raise VendorUnavailableError("down")
        return [_item("Bharti Airtel deal", "2026-11-05")]

    monkeypatch.setattr(india_rss, "_fetch", fetch)
    assert "Bharti Airtel deal" in india_rss.get_news("BHARTIARTL.NS", "2026-11-01", "2026-11-06")


@pytest.mark.unit
def test_us_stocks_are_not_searched_in_indian_feeds(feeds):
    with pytest.raises(NoMarketDataError):
        india_rss.get_news("AAPL", "2026-11-01", "2026-11-06")


@pytest.mark.unit
def test_an_indian_stock_is_discussed_in_indian_subreddits_by_its_code(monkeypatch):
    assert reddit.subreddits_for("RELIANCE.NS") == reddit.INDIA_SUBREDDITS
    assert reddit.subreddits_for("AAPL") == reddit.DEFAULT_SUBREDDITS
    asked = []
    monkeypatch.setattr(reddit, "_fetch_subreddit_rss", lambda ticker, *a, **k: asked.append(ticker))
    reddit.fetch_reddit_posts("RELIANCE.NS")
    assert asked == ["RELIANCE"]


@pytest.mark.unit
def test_an_indian_stock_skips_stocktwits(monkeypatch):
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("StockTwits asked")))
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "posts")
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda t: None)
    monkeypatch.setattr(sentiment_analyst.get_news, "func", lambda *a: "news")
    monkeypatch.setattr(sentiment_analyst, "bind_structured", lambda *a: None)
    prompts = []
    monkeypatch.setattr(sentiment_analyst, "invoke_structured_or_freetext",
                        lambda s, llm, messages, *a: prompts.append(messages[0].content) or "report")
    node = sentiment_analyst.create_sentiment_analyst(object())
    node({"company_of_interest": "RELIANCE.NS", "trade_date": "2026-11-06", "messages": []})
    assert "no meaningful coverage of NSE/BSE" in prompts[0]
    assert "r/IndianStreetBets is speculative" in prompts[0]


@pytest.mark.integration
def test_live_indian_feeds_parse():
    assert india_rss._fetch(india_rss.FEEDS["Business Standard Markets"])
