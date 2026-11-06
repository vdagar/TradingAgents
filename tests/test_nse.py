"""NSE vendor: the MCP price feed, nseindia.com filings, and their point-in-time rules."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import nse


class _Resp:
    def __init__(self, payload, status=200, sse=True, headers=None):
        self.status_code = status
        body = json.dumps(payload)
        self.text = f"event:message\ndata:{body}\n" if sse else body
        self.headers = {"Content-Type": "text/event-stream" if sse else "application/json",
                        "Mcp-Session-Id": "s1", **(headers or {})}
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def _tool_result(data, is_error=False):
    return {"jsonrpc": "2.0", "id": 2,
            "result": {"content": [{"type": "text", "text": json.dumps(data)}], "isError": is_error}}


def _mcp(monkeypatch, *answers):
    """Script requests.post: every initialize succeeds, tools/call returns ``answers`` in turn."""
    answers = list(answers)

    def post(url, json=None, headers=None, timeout=None):
        if json["method"] == "initialize":
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": {}}, sse=False)
        return answers.pop(0)

    monkeypatch.setattr(nse.requests, "post", post)
    return answers


@pytest.mark.unit
def test_a_call_landing_on_a_server_without_the_session_is_retried(monkeypatch):
    left = _mcp(monkeypatch, _Resp({}, status=404), _Resp(_tool_result({"ok": 1})))
    assert nse.mcp_call(nse.BHAVCOPY_MCP, "t", {}) == {"ok": 1}
    assert left == []


@pytest.mark.unit
def test_a_tool_error_is_a_vendor_outage_not_an_absence(monkeypatch):
    _mcp(monkeypatch, _Resp(_tool_result("boom", is_error=True)))
    with pytest.raises(VendorUnavailableError):
        nse.mcp_call(nse.BHAVCOPY_MCP, "t", {})


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["AAPL", "500325.BO", "^NSEI"])
def test_only_nse_symbols_are_served(symbol):
    with pytest.raises(NoMarketDataError):
        nse.nse_code(symbol)


@pytest.mark.unit
def test_a_dual_listed_bse_symbol_reads_nse():
    assert nse.nse_code("reliance.bo") == "RELIANCE"


def _bar(day, close):
    return {"date": day, "open": close, "high": close, "low": close, "close": close, "volume": 10}


@pytest.mark.unit
def test_prices_are_chained_back_and_trimmed_to_the_window(monkeypatch):
    _mcp(monkeypatch,
         _Resp(_tool_result({"data": [_bar("2026-10-01", 3), _bar("2026-10-20", 4)], "next_end_date": "2026-07-01"})),
         _Resp(_tool_result({"data": [_bar("2026-06-15", 1), _bar("2026-07-01", 2)], "next_end_date": "2026-04-01"})))
    out = nse.get_stock_data("RELIANCE.NS", "2026-07-01", "2026-10-10")
    assert "Date,Open,High,Low,Close,Volume" in out
    assert "2026-07-01" in out and "2026-10-01" in out
    assert "2026-06-15" not in out and "2026-10-20" not in out
    assert "Total records: 2" in out


def _site(monkeypatch, routes):
    def get(url, params=None, timeout=None, headers=None):
        return _Resp(routes[url.removeprefix(nse.SITE)], sse=False)
    monkeypatch.setattr(nse.requests, "get", get)
    monkeypatch.setattr(nse, "cached_json", lambda namespace, key, ttl, fetch: fetch())
    monkeypatch.setattr(nse, "_MIN_GAP", 0)


@pytest.mark.unit
def test_insider_trades_disclosed_after_the_run_date_are_not_served(monkeypatch):
    _site(monkeypatch, {"/api/corporates-pit": {"data": [
        {"date": "06-Apr-2026 16:07", "acqName": "Before", "secAcq": "1"},
        {"date": "20-Apr-2026 10:00", "acqName": "After", "secAcq": "1"},
    ]}})
    out = nse.get_insider_transactions("INFY.NS", "2026-04-10")
    assert "Before" in out and "After" not in out


@pytest.mark.unit
def test_shareholding_is_as_published_by_the_run_date(monkeypatch):
    _site(monkeypatch, {
        "/api/corporate-share-holdings-master": [
            {"date": "30-SEP-2026", "broadcastDate": "21-OCT-2026 16:25:15", "pr_and_prgrp": "50.44", "public_val": "49.56"},
            {"date": "30-JUN-2026", "broadcastDate": "16-JUL-2026 10:00:00", "pr_and_prgrp": "50.48", "public_val": "49.52"},
        ],
        "/api/corporate-pledgedata": {"data": [
            {"broadcastDt": "06-Nov-2026 16:30:28", "shp": "30-Sep-2026", "percPromoterHolding": " 51.25",
             "percSharesPledged": "1.52", "numSharesPledged": "205501930"}]},
    })
    out = nse.get_shareholding("RELIANCE.NS", "2026-10-01")
    assert "50.48" in out and "50.44" not in out
    assert "No pledge disclosure published by this date." in out


@pytest.mark.unit
def test_a_past_run_gets_no_live_option_chain(monkeypatch):
    monkeypatch.setattr(nse.requests, "get", mock.Mock(side_effect=AssertionError("fetched")))
    out = nse.get_option_chain("RELIANCE.NS", "2020-01-01")
    assert "unavailable for historical date 2020-01-01" in out


@pytest.mark.unit
def test_option_chain_summary():
    def row(strike, ce, pe):
        return {"strikePrice": strike, "CE": {"openInterest": ce, "underlyingValue": 105},
                "PE": {"openInterest": pe, "underlyingValue": 105}}

    out = nse.summarize_option_chain("X.NS", "23-Nov-2026", [row(90, 0, 300), row(100, 100, 100), row(110, 400, 0)])
    assert "Put-call ratio (open interest): 0.80" in out
    assert "Max pain strike: 100" in out
    assert "Highest call OI (resistance): 110 (400)" in out


@pytest.mark.unit
def test_an_nse_outage_falls_back_to_yahoo_prices(monkeypatch):
    def down(*a, **k):
        raise VendorUnavailableError("NSE down")

    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "nse_mcp", down)
    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "yfinance", lambda *a, **k: "yahoo prices")
    assert router.route_to_vendor("get_stock_data", "RELIANCE.NS", "2026-01-01", "2026-01-10") == "yahoo prices"


@pytest.mark.unit
def test_a_us_stock_gets_no_data_not_a_crash_from_india_tools():
    out = router.route_to_vendor("get_shareholding", "AAPL", "2026-01-10")
    assert out.startswith("NO_DATA_AVAILABLE") and "not an NSE-listed symbol" in out


@pytest.mark.integration
def test_live_nse_prices():
    out = nse.get_stock_data("RELIANCE.NS", "2026-01-01", "2026-02-01")
    assert "Total records:" in out
