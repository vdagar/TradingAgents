"""Indian financial news from Economic Times and Business Standard RSS feeds.

The feeds carry their latest ~50 headlines each, unfiltered by company, so a
ticker's news is the headlines that name the company or its NSE code. A window
the feeds no longer reach is reported as unavailable, not as no news.
"""

from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import cached_json
from tradingagents.dataflows.symbols import is_indian_symbol
from tradingagents.dataflows.vendors.yahoo.fundamentals import get_company_profile

FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Economic Times Stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard Companies": "https://www.business-standard.com/rss/companies-101.rss",
}
_UA = "tradingagents/0.2"  # Business Standard refuses a User-Agent carrying a URL
_TTL = 15 * 60
_NAME_NOISE = {"limited", "ltd", "ltd.", "the", "india", "industries", "corporation", "company"}


def _fetch(url: str) -> list[dict]:
    try:
        response = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as exc:
        raise VendorUnavailableError(f"RSS feed {url} failed: {exc}") from exc
    return [{"title": (i.findtext("title") or "").strip(),
             "summary": re.sub(r"<[^>]+>", " ", i.findtext("description") or "").strip(),
             "link": (i.findtext("link") or "").strip(),
             "published": i.findtext("pubDate") or ""} for i in root.iter("item")]


def _items() -> list[tuple[str, dict]]:
    """Every feed's items, tagged with the feed's name; a feed that fails is skipped."""
    items, failures = [], []
    for source, url in FEEDS.items():
        try:
            items += [(source, i) for i in cached_json("india_rss", url, _TTL, lambda u=url: _fetch(u))]
        except VendorUnavailableError as exc:
            failures.append(exc)
    if not items and failures:
        raise failures[0]
    return items


def _published(item: dict) -> datetime | None:
    try:
        return parsedate_to_datetime(item["published"])
    except (TypeError, ValueError):
        return None


@functools.lru_cache(maxsize=256)
def _names(ticker: str) -> tuple[str, ...]:
    """What a headline calls the company: its NSE code and its name without legal noise."""
    code = ticker.upper().rsplit(".", 1)[0]
    names = [code] if not code.isdigit() else []
    try:
        name = get_company_profile(ticker).get("longName") or ""
    except Exception:  # noqa: BLE001 — the code alone still matches
        name = ""
    words = [w for w in name.split() if w.lower() not in _NAME_NOISE]
    if words:
        names.append(" ".join(words[:2]))
    return tuple(names)


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Headlines in the window that name the company."""
    if not is_indian_symbol(ticker):
        raise NoMarketDataError(ticker, detail="Indian news feeds cover NSE/BSE companies only")
    names = _names(ticker)
    pattern = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names), re.IGNORECASE) if names else None
    items = _items()
    start_dt, end_dt = datetime.strptime(start_date, "%Y-%m-%d"), datetime.strptime(end_date, "%Y-%m-%d")
    hits = [(s, i) for s, i in items
            if pattern and pattern.search(f"{i['title']} {i['summary']}")
            and in_window(_published(i), start_dt, end_dt)]
    if not hits:
        gap = coverage_gap((_published(i) for _, i in items), start_date, end_date,
                           "Indian news feeds", f"news for {ticker}")
        # Nothing names it: let the next vendor (Yahoo) try before saying so.
        raise NoMarketDataError(ticker, detail=gap or f"no ET/Business Standard headline names {', '.join(names)}")
    out = f"## {ticker} News (Economic Times, Business Standard), from {start_date} to {end_date}:\n\n"
    seen = set()
    for source, i in hits:
        if i["link"] in seen:
            continue
        seen.add(i["link"])
        out += f"### {i['title']} (source: {source})\n{i['summary'][:400]}\nLink: {i['link']}\n\n"
    return out
