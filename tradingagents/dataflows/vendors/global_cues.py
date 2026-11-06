"""Global cues for Indian equities: world markets, the dollar, rupee, yields and
commodities as of the run date, plus FII/DII flows and GIFT Nifty when live."""

from __future__ import annotations

from datetime import datetime, timedelta

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.net import cached_json
from tradingagents.dataflows.vendors import nse
from tradingagents.dataflows.vendors.yahoo.market import get_closes

NSEIX_WATCH = "https://www.nseix.com/api/derivatives-watch"


def _change(closes, days_back: int) -> str:
    if len(closes) <= days_back:
        return "n/a"
    return f"{(closes.iloc[-1] / closes.iloc[-1 - days_back] - 1) * 100:+.2f}%"


def _markets(as_of_date: str) -> list[str]:
    start = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
    end = (datetime.strptime(as_of_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    lines = ["| Market | Symbol | Last close | Date | 1D | 5D | 1M | Read-through to India |",
             "|---|---|---|---|---|---|---|---|"]
    for symbol, label, why in get_config()["global_cues"]:
        try:
            closes = get_closes(symbol, start, end).dropna()
        except Exception as exc:  # noqa: BLE001 — one quote failing must not drop the table
            lines.append(f"| {label} | {symbol} | unavailable ({type(exc).__name__}) | | | | | {why} |")
            continue
        if closes.empty:
            lines.append(f"| {label} | {symbol} | unavailable | | | | | {why} |")
            continue
        lines.append(f"| {label} | {symbol} | {closes.iloc[-1]:,.2f} | {closes.index[-1]:%Y-%m-%d} | "
                     f"{_change(closes, 1)} | {_change(closes, 5)} | {_change(closes, 21)} | {why} |")
    return lines


def _fii_dii() -> list[str]:
    rows = nse.site_json("/api/fiidiiTradeReact", {}, nse.INTRADAY_TTL) or []
    return [f"- {r.get('category')} on {r.get('date')}: bought INR {r.get('buyValue')} cr, sold INR "
            f"{r.get('sellValue')} cr, net INR {r.get('netValue')} cr" for r in rows]


def _gift_nifty() -> list[str]:
    def fetch():
        try:
            response = requests.get(NSEIX_WATCH, params={"inst_type1": "IDX", "inst_type2": "STK", "type": "live"},
                                    headers={"User-Agent": nse._UA, "Referer": "https://www.nseix.com/"}, timeout=20)
            response.raise_for_status()
            return response.json().get("data") or []
        except (requests.RequestException, ValueError) as exc:
            raise VendorUnavailableError(f"NSE IX failed: {exc}") from exc

    futures = [r for r in cached_json("nseix", "derivatives-watch", nse.INTRADAY_TTL, fetch)
               if r.get("SYMBOL") == "NIFTY" and "Fut" in str(r.get("INSTRUMENT"))]
    return [f"- GIFT Nifty {r.get('EXPIRYDATE')} future: {r.get('LASTPRICE')} ({r.get('PERCHANGE')}%)"
            for r in futures[:1]]


def get_global_cues(as_of_date: str) -> str:
    out = [f"# Global cues for Indian equities as of {as_of_date}", "", *_markets(as_of_date), ""]
    for title, live in (("## FII/DII cash-market flows (NSE, provisional)", _fii_dii),
                        ("## GIFT Nifty (NSE IX, trades while India is closed)", _gift_nifty)):
        out.append(title)
        if is_historical(as_of_date):
            out.append(f"unavailable for historical date {as_of_date}: only the latest values are published.")
            continue
        try:
            out += live() or ["unavailable: no data published."]
        except VendorUnavailableError as exc:
            out.append(f"unavailable: {exc}")
    return "\n".join(out)
