"""Screener vendor: the pinned stdio MCP server, its formatting, and point-in-time rules."""

from __future__ import annotations

import copy

import pytest

from tradingagents.dataflows import config as config_module, router
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import screener
from tradingagents.default_config import DEFAULT_CONFIG

QUARTERS = {"statements": "consolidated", "columns": ["Jun 2026", "Sep 2026"],
            "rows": [{"label": "Sales", "values": [72275, 73188]}, {"label": "EPS", "values": [None, 35.1]}]}


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(screener, "_call", lambda tool, args: calls.append((tool, args)) or QUARTERS)
    monkeypatch.setattr(screener, "cached_json", lambda namespace, key, ttl, fetch: fetch())
    return calls


@pytest.mark.unit
def test_quarterly_results_read_as_a_table_in_crore(served):
    out = screener.get_income_statement("TCS.NS", "quarterly", None)
    assert served == [("get_quarterly_results", {"symbol": "TCS"})]
    assert "INR crore" in out and "| Sales | 72275 | 73188 |" in out and "| EPS |  | 35.1 |" in out


@pytest.mark.unit
def test_annual_income_reads_the_profit_and_loss(served):
    screener.get_income_statement("TCS.NS", "annual", None)
    assert served[0][0] == "get_profit_loss"


@pytest.mark.unit
def test_a_past_run_is_not_served_todays_statements(served):
    out = screener.get_balance_sheet("TCS.NS", "annual", "2020-01-01")
    assert "withheld" in out and served == []


@pytest.mark.unit
def test_a_non_indian_symbol_is_no_data(served):
    with pytest.raises(NoMarketDataError):
        screener.get_cashflow("AAPL", "annual", None)


@pytest.mark.unit
def test_no_uv_means_not_configured(monkeypatch):
    monkeypatch.setattr(screener.shutil, "which", lambda name: None)
    with pytest.raises(VendorNotConfiguredError):
        screener._call("get_company_overview", {"symbol": "TCS"})


@pytest.mark.unit
def test_a_tampered_server_download_is_never_run(monkeypatch, tmp_path):
    config_module._config = {**copy.deepcopy(DEFAULT_CONFIG), "data_cache_dir": str(tmp_path)}
    monkeypatch.setattr(screener.requests, "get", lambda *a, **k: type("R", (), {"content": b"evil"})())
    with pytest.raises(VendorUnavailableError, match="SHA-256"):
        screener._server_path()
    assert not list(tmp_path.rglob("*.py"))


@pytest.mark.unit
def test_india_fundamentals_try_screener_then_yahoo(monkeypatch):
    def down(*a, **k):
        raise VendorNotConfiguredError("no uv")

    monkeypatch.setitem(router.VENDOR_METHODS["get_fundamentals"], "screener", down)
    monkeypatch.setitem(router.VENDOR_METHODS["get_fundamentals"], "yfinance", lambda *a, **k: "yahoo profile")
    assert router.route_to_vendor("get_fundamentals", "TCS.NS", "2026-11-06") == "yahoo profile"


@pytest.mark.integration
def test_live_screener_overview():
    assert "Key ratios" in screener.get_fundamentals("TCS.NS")
