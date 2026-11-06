"""NSE India: prices from NSE's own MCP server, and filings from nseindia.com.

Prices come from NSE's public MCP servers (Streamable HTTP, no key). The protocol
is JSON-RPC over POST, so it is spoken here with ``requests`` rather than an
async SDK. Insider (PIT) disclosures, shareholding, promoter pledges, corporate
announcements and the option chain come from the JSON endpoints behind
nseindia.com's own pages.

NSE serves this data for information and learning, not for commercial use; see
https://www.nseindia.com/nse-mcp.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import requests

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import cached_json
from tradingagents.dataflows.symbols import is_indian_symbol

BHAVCOPY_MCP = "https://mcp.nseindia.in/bhavcopy/cm/mcp"
SITE = "https://www.nseindia.com"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
INTRADAY_TTL = 15 * 60
EOD_TTL = 24 * 60 * 60

_lock = threading.Lock()
_last_call = 0.0
_MIN_GAP = 1.0  # seconds between NSE requests


def nse_code(symbol: str) -> str:
    """NSE's symbol for ``RELIANCE.NS`` (or a dual-listed ``RELIANCE.BO``).

    A numeric BSE scrip code has no NSE symbol, so it is no NSE data.
    """
    base = symbol.strip().upper().rsplit(".", 1)[0]
    if not is_indian_symbol(symbol) or base.isdigit():
        raise NoMarketDataError(symbol, base, "not an NSE-listed symbol")
    return base


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_GAP - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _post(url: str, body: dict, session_id: str | None) -> requests.Response:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(url, json=body, headers=headers, timeout=30)


def _rpc_result(response: requests.Response) -> dict:
    """The JSON-RPC result, from a JSON or a server-sent-events body."""
    text = response.text
    if "event-stream" in response.headers.get("Content-Type", ""):
        text = "".join(line[5:] for line in text.splitlines() if line.startswith("data:"))
    message = json.loads(text)
    if "error" in message:
        raise VendorUnavailableError(f"NSE MCP error: {message['error'].get('message')}")
    return message["result"]


# NSE balances its MCP across servers that do not share sessions, so a call can
# land on one that never saw the session (404); a fresh session usually lands.
_MCP_ATTEMPTS = 6


def mcp_call(server: str, tool: str, arguments: dict) -> dict:
    """Call ``tool`` on an NSE MCP server; its JSON answer as a dict."""
    init_body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "tradingagents", "version": "1"}}}
    call_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": tool, "arguments": arguments}}
    try:
        for _ in range(_MCP_ATTEMPTS):
            init = _post(server, init_body, None)
            init.raise_for_status()
            response = _post(server, call_body, init.headers.get("Mcp-Session-Id"))
            if response.status_code != 404:
                break
        response.raise_for_status()
        result = _rpc_result(response)
    except (requests.RequestException, ValueError) as exc:
        raise VendorUnavailableError(f"NSE MCP {tool} failed: {exc}") from exc
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise VendorUnavailableError(f"NSE MCP {tool} failed: {text[:200]}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise VendorUnavailableError(f"NSE MCP {tool} returned unreadable data") from exc


def site_json(path: str, params: dict, ttl: float):
    """A nseindia.com JSON endpoint's answer, cached for ``ttl`` seconds."""
    def fetch():
        _throttle()
        try:
            response = requests.get(f"{SITE}{path}", params=params, timeout=20, headers={
                "User-Agent": _UA, "Accept": "application/json", "Referer": f"{SITE}/"})
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise VendorUnavailableError(f"nseindia.com {path} failed: {exc}") from exc

    return cached_json("nse", f"{path}?{sorted(params.items())}", ttl, fetch)


def _day(text: str | None) -> str:
    """NSE's ``06-Apr-2026 16:07`` / ``30-SEP-2026`` as ``2026-04-06``; '' if unreadable."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime((text or "").strip().title(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def withheld(title: str, label: str, as_of_date: str) -> str:
    """What a live-only India tool serves to a run dated in the past."""
    return (
        f"# {title} for {label}\n"
        f"unavailable for historical date {as_of_date}: this source serves only the latest "
        f"values, which postdate the analysis date. Proceed without it; do not estimate it."
    )


# --- prices (NSE MCP) ----------------------------------------------------------------

def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV from NSE's bhavcopy MCP, chained 3 months a call."""
    code = nse_code(symbol)
    rows, end = [], end_date
    while end >= start_date:
        chunk = mcp_call(BHAVCOPY_MCP, "get_stock_history", {"symbol": code, "months": 3, "endDate": end})
        data = chunk.get("data") or []
        rows += [r for r in data if start_date <= r.get("date", "") <= end_date]
        following = chunk.get("next_end_date")
        if not data or not following or following >= end:
            break
        end = following
    if not rows:
        raise NoMarketDataError(symbol, code, f"no NSE rows between {start_date} and {end_date}")
    rows.sort(key=lambda r: r["date"])
    lines = ["Date,Open,High,Low,Close,Volume"]
    lines += [f"{r['date']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}" for r in rows]
    return (
        f"# Stock data for {symbol} (NSE: {code}) from {start_date} to {end_date}, in INR\n"
        f"# Total records: {len(rows)}. Source: NSE bhavcopy; prices are not adjusted for splits "
        f"or bonus issues (see corporate actions).\n\n" + "\n".join(lines) + "\n"
    )


# --- filings (nseindia.com) ----------------------------------------------------------

def get_insider_transactions(ticker: str, as_of_date: str | None = None) -> str:
    """SEBI PIT disclosures made public by ``as_of_date``."""
    code = nse_code(ticker)
    as_of_date = as_of_date or get_current_date()
    rows = site_json("/api/corporates-pit", {"index": "equities", "symbol": code}, EOD_TTL).get("data") or []
    rows = [r for r in rows if _day(r.get("date")) and _day(r.get("date")) <= as_of_date]
    header = f"# Insider (SEBI PIT) disclosures for {ticker}, public by {as_of_date}\n"
    if not rows:
        return header + "No PIT disclosures in NSE's recent list."
    lines = ["| Disclosed | Person | Category | Type | Mode | Shares | Value (INR) | Holding after |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows[:25]:
        lines.append(f"| {_day(r.get('date'))} | {r.get('acqName')} | {r.get('personCategory')} | "
                     f"{r.get('tdpTransactionType')} | {r.get('acqMode')} | {r.get('secAcq')} | "
                     f"{r.get('secVal')} | {r.get('afterAcqSharesPer')}% |")
    return header + "\n".join(lines)


def get_shareholding(ticker: str, as_of_date: str) -> str:
    """Promoter vs public holding by quarter, and promoter pledges, as published by ``as_of_date``."""
    code = nse_code(ticker)
    quarters = site_json("/api/corporate-share-holdings-master",
                         {"index": "equities", "symbol": code}, EOD_TTL) or []
    quarters = [q for q in quarters if _day(q.get("broadcastDate")) and _day(q.get("broadcastDate")) <= as_of_date]
    out = [f"# Shareholding for {ticker}, as published by {as_of_date}", "",
           "| Quarter end | Promoter & group % | Public % | Published |", "|---|---|---|---|"]
    out += [f"| {_day(q.get('date'))} | {q.get('pr_and_prgrp')} | {q.get('public_val')} | "
            f"{_day(q.get('broadcastDate'))} |" for q in quarters[:8]]
    if not quarters:
        out.append("| none published by this date | | | |")
    pledges = site_json("/api/corporate-pledgedata", {"index": "equities", "symbol": code}, EOD_TTL)
    pledges = [p for p in (pledges or {}).get("data") or []
               if _day(p.get("broadcastDt")) and _day(p.get("broadcastDt")) <= as_of_date]
    out += ["", "## Promoter pledges"]
    if pledges:
        p = pledges[0]
        out.append(f"As of {p.get('shp')} (published {_day(p.get('broadcastDt'))}): promoters hold "
                   f"{str(p.get('percPromoterHolding')).strip()}% of the company; "
                   f"{str(p.get('percSharesPledged')).strip()}% of promoter shares are pledged "
                   f"({p.get('numSharesPledged')} shares).")
    else:
        out.append("No pledge disclosure published by this date.")
    return "\n".join(out)


def get_corporate_announcements(ticker: str, as_of_date: str, look_back_days: int = 30) -> str:
    """Exchange filings in the ``look_back_days`` to ``as_of_date``, and two years of corporate actions."""
    code = nse_code(ticker)
    start = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    items = site_json("/api/corporate-announcements", {"index": "equities", "symbol": code}, INTRADAY_TTL) or []
    items = [a for a in items if start <= _day(a.get("an_dt")) <= as_of_date]
    out = [f"# NSE announcements for {ticker}, {start} to {as_of_date}"]
    out += [f"- {_day(a.get('an_dt'))} | {a.get('desc')}: {(a.get('attchmntText') or '').strip()[:300]}"
            for a in items[:30]] or ["No announcements in this window."]
    actions_from = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=730)).strftime("%Y-%m-%d")
    try:
        actions = mcp_call(BHAVCOPY_MCP, "get_corporate_actions",
                           {"symbol": code, "fromDate": actions_from, "toDate": as_of_date})
        listed = actions.get("data") or actions.get("corporate_actions") or actions.get("actions") or []
        out += ["", "## Corporate actions (ex-date in the last two years)"]
        out += [f"- {a.get('exDate')} {a.get('actionType')}: {a.get('purpose')}" for a in listed[:20]] or ["None."]
    except VendorUnavailableError as exc:
        out += ["", f"## Corporate actions\nunavailable: {exc}"]
    return "\n".join(out)


def get_option_chain(ticker: str, as_of_date: str) -> str:
    """Nearest-expiry option chain summary: PCR, max pain, and the strikes with most open interest."""
    code = nse_code(ticker)
    if is_historical(as_of_date):
        return withheld("Option chain", ticker, as_of_date)
    info = site_json("/api/option-chain-contract-info", {"symbol": code}, INTRADAY_TTL) or {}
    expiries = info.get("expiryDates") or []
    if not expiries:
        return f"# Option chain for {ticker}\nunavailable: {code} has no stock options on NSE (not in F&O)."
    expiry = expiries[0]
    chain = site_json("/api/option-chain-v3", {"type": "Equity", "symbol": code, "expiry": expiry}, INTRADAY_TTL)
    return summarize_option_chain(ticker, expiry, (chain or {}).get("records", {}).get("data") or [])


def summarize_option_chain(ticker: str, expiry: str, rows: list[dict]) -> str:
    """PCR by open interest, max pain, and the top call / put open-interest strikes."""
    calls = {r["strikePrice"]: r["CE"].get("openInterest", 0) or 0 for r in rows if r.get("CE")}
    puts = {r["strikePrice"]: r["PE"].get("openInterest", 0) or 0 for r in rows if r.get("PE")}
    if not calls or not puts:
        return f"# Option chain for {ticker} ({expiry})\nunavailable: NSE returned no open interest."
    spot = next((r[s].get("underlyingValue") for r in rows for s in ("CE", "PE") if r.get(s)), None)
    strikes = sorted(set(calls) | set(puts))
    # ponytail: O(n^2) over strikes, fine for the ~100 strikes of one expiry
    pain = min(strikes, key=lambda k: sum(calls.get(s, 0) * max(0, k - s) + puts.get(s, 0) * max(0, s - k)
                                          for s in strikes))
    pcr = sum(puts.values()) / max(1, sum(calls.values()))
    top = lambda oi: ", ".join(f"{k:g} ({v:,})" for k, v in sorted(oi.items(), key=lambda kv: -kv[1])[:3])  # noqa: E731
    return (
        f"# Option chain for {ticker}, expiry {expiry} (NSE, live)\n"
        f"Underlying: {spot}\n"
        f"Put-call ratio (open interest): {pcr:.2f}\n"
        f"Max pain strike: {pain:g}\n"
        f"Highest call OI (resistance): {top(calls)}\n"
        f"Highest put OI (support): {top(puts)}\n"
    )
