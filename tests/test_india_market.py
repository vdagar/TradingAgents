"""The Indian market: the default market, its vendor chains, symbols, and prompt context."""

from __future__ import annotations

import pytest

from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _graph, offline  # noqa: F401
from tradingagents.agents.context import INDIA_MARKET_CONTEXT, build_instrument_context
from tradingagents.dataflows.symbols import is_indian_symbol, market_symbol
from tradingagents.default_config import DEFAULT_CONFIG, MARKET_PROFILES, build_default_config


@pytest.mark.unit
def test_india_is_the_default_market():
    assert DEFAULT_CONFIG["market"] == "IN"
    assert DEFAULT_CONFIG["data_vendors"] == MARKET_PROFILES["IN"]["data_vendors"]
    assert "sec_edgar" not in DEFAULT_CONFIG["data_vendors"]["fundamental_data"]


@pytest.mark.unit
def test_the_market_comes_from_the_argument_then_the_environment(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MARKET", "us")
    assert build_default_config()["data_vendors"] == MARKET_PROFILES["US"]["data_vendors"]
    assert build_default_config("IN")["market"] == "IN"
    with pytest.raises(ValueError, match="unknown market"):
        build_default_config("JP")


@pytest.mark.unit
def test_profiles_are_copied_not_shared():
    build_default_config("IN")["data_vendors"]["core_stock_apis"] = "x"
    assert MARKET_PROFILES["IN"]["data_vendors"]["core_stock_apis"] == "nse_mcp,yfinance"


@pytest.mark.unit
@pytest.mark.parametrize("raw, market, expected", [
    ("reliance", "IN", "RELIANCE.NS"),
    ("BAJAJ-AUTO", "IN", "BAJAJ-AUTO.NS"),
    ("TCS.NS", "IN", "TCS.NS"),
    ("500325.bo", "IN", "500325.BO"),
    ("^NSEI", "IN", "^NSEI"),
    ("USDINR", "IN", "USDINR=X"),
    ("BTC-USD", "IN", "BTC-USD"),
    ("XAUUSD", "IN", "GC=F"),
    ("AAPL", "US", "AAPL"),
])
def test_a_bare_code_is_listed_on_nse_in_the_india_market(raw, market, expected):
    assert market_symbol(raw, market) == expected


@pytest.mark.unit
def test_only_nse_and_bse_symbols_are_indian():
    assert is_indian_symbol("TCS.NS") and is_indian_symbol("500325.bo")
    assert not is_indian_symbol("AAPL") and not is_indian_symbol("^NSEI")


@pytest.mark.unit
def test_an_indian_instrument_carries_the_india_context():
    assert INDIA_MARKET_CONTEXT in build_instrument_context("RELIANCE.NS")
    assert "INR" not in build_instrument_context("AAPL")
    assert INDIA_MARKET_CONTEXT not in build_instrument_context("BTC-USD", "crypto")


@pytest.mark.unit
def test_the_cli_run_config_takes_the_chosen_markets_vendors():
    from cli import run

    selections = {"market": "US", "research_depth": 1, "quick_think_llm": "q", "deep_think_llm": "d",
                  "backend_url": None, "llm_provider": "openai", "output_language": "English"}
    config = run._build_run_config(selections, None)
    assert config["market"] == "US"
    assert config["data_vendors"] == MARKET_PROFILES["US"]["data_vendors"]
    assert config["global_news_queries"] == MARKET_PROFILES["US"]["global_news_queries"]


@pytest.mark.unit
def test_the_market_flag_reaches_the_run(monkeypatch):
    from typer.testing import CliRunner

    from cli import main as cli_main

    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    assert CliRunner().invoke(cli_main.app, ["--market", "US"]).exit_code == 0
    assert seen["flags"]["market"] == "US"


@pytest.mark.unit
def test_a_full_run_on_an_nse_stock_reaches_a_decision(tmp_path, monkeypatch, offline):  # noqa: F811
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())

    state, signal = graph.propagate("RELIANCE.NS", TRADE_DATE)

    assert signal == "Overweight"
    assert INDIA_MARKET_CONTEXT in state["instrument_context"]
