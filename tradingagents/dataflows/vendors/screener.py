"""Screener.in fundamentals through the community screener-mcp server, over stdio.

The server is one MIT-licensed file run by ``uv run --script`` (it declares its
own dependencies), fetched once from a pinned commit and checked against its
SHA-256 before it is ever run. Without ``uv`` on PATH the vendor reports itself
not configured and the router moves on to the next one (Yahoo).

Screener serves current figures and statements dated by the period they cover,
not by when they were filed, so a run dated in the past is told the data is
withheld, as Yahoo's statements are.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import (
    withhold_live_profile,
    withhold_undated_statements,
)
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.net import cached_json
from tradingagents.dataflows.symbols import is_indian_symbol

_COMMIT = "e84afa0f00793dbf198d3d9eaeb247571622b1cc"
SERVER_URL = f"https://raw.githubusercontent.com/gaharivatsa/screener-mcp/{_COMMIT}/server.py"
SERVER_SHA256 = "8ecb755b82b4e45e417422e40ae79e713fb2d972088c5325a1fc545c09dbee4b"
_TIMEOUT = 180  # seconds; the first run also installs the server's dependencies
_TTL = 24 * 60 * 60


def _server_path() -> Path:
    """The pinned server file, downloaded once and verified."""
    path = Path(get_config()["data_cache_dir"]) / "screener-mcp" / f"server-{_COMMIT[:12]}.py"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == SERVER_SHA256:
        return path
    try:
        body = requests.get(SERVER_URL, timeout=30).content
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"could not fetch screener-mcp: {exc}") from exc
    if hashlib.sha256(body).hexdigest() != SERVER_SHA256:
        raise VendorUnavailableError("screener-mcp download does not match its pinned SHA-256; not running it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _call(tool: str, arguments: dict) -> dict:
    """One tool call on a fresh stdio server; its JSON answer."""
    uv = shutil.which("uv")
    if not uv:
        raise VendorNotConfiguredError("screener vendor needs `uv` on PATH to run screener-mcp")
    proc = subprocess.Popen([uv, "run", "--quiet", "--script", str(_server_path())],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(_TIMEOUT, proc.kill)
    timer.start()
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "tradingagents", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        ):
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            reply = json.loads(line)
            if reply.get("id") == 2:
                break
        else:
            raise VendorUnavailableError(f"screener-mcp exited without answering {tool}")
    except (OSError, ValueError) as exc:
        raise VendorUnavailableError(f"screener-mcp {tool} failed: {exc}") from exc
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
    if "error" in reply:
        raise VendorUnavailableError(f"screener-mcp {tool}: {reply['error'].get('message')}")
    result = reply["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        # The server names a symbol it cannot resolve; anything else is an outage.
        if "not found" in text.lower() or "did you mean" in text.lower():
            raise NoMarketDataError(arguments.get("symbol", ""), detail=text[:200])
        raise VendorUnavailableError(f"screener-mcp {tool}: {text[:200]}")
    return result.get("structuredContent") or json.loads(text)


def _screener(tool: str, ticker: str) -> dict:
    if not is_indian_symbol(ticker):
        raise NoMarketDataError(ticker, detail="screener.in covers NSE/BSE listings only")
    symbol = ticker.strip().upper().rsplit(".", 1)[0]
    return cached_json("screener", f"{tool}:{symbol}", _TTL, lambda: _call(tool, {"symbol": symbol}))


def _table(data: dict, title: str, ticker: str) -> str:
    """A screener statement (columns, labelled rows) as a markdown table, in INR crore."""
    columns, rows = data.get("columns") or [], data.get("rows") or []
    if not rows:
        raise NoMarketDataError(ticker, detail=f"screener.in has no {title}")
    lines = [f"# {title} for {ticker} (screener.in, {data.get('statements', 'consolidated')}; INR crore)", "",
             "| Item | " + " | ".join(columns) + " |", "|---" * (len(columns) + 1) + "|"]
    lines += ["| " + r["label"] + " | " + " | ".join("" if v is None else str(v) for v in r["values"]) + " |"
              for r in rows]
    growth = data.get("growth_metrics")
    if growth:
        lines += ["", "## Growth", json.dumps(growth)]
    return "\n".join(lines)


def get_fundamentals(ticker: str, as_of_date: str | None = None) -> str:
    withheld = withhold_live_profile(as_of_date, ticker)
    if withheld:
        return withheld
    d = _screener("get_company_overview", ticker)
    ratios = "\n".join(f"- {r['name']}: {r['value']}" for r in d.get("top_ratios") or [])
    pros = "\n".join(f"- {p}" for p in d.get("pros") or [])
    cons = "\n".join(f"- {c}" for c in d.get("cons") or [])
    return (
        f"# Company Fundamentals for {ticker} (screener.in)\n"
        f"Sector: {d.get('sector')}; Industry: {d.get('industry')}\n\n"
        f"## Key ratios\n{ratios}\n\n## About\n{d.get('about') or ''}\n\n"
        f"## Pros (machine-generated)\n{pros}\n\n## Cons (machine-generated)\n{cons}\n"
    )


def _statement(ticker: str, as_of_date: str | None, title: str, tool: str) -> str:
    withheld = withhold_undated_statements(as_of_date, ticker, title)
    if withheld:
        return withheld
    return _table(_screener(tool, ticker), title, ticker)


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement(ticker, as_of_date, "Balance Sheet (annual)", "get_balance_sheet")


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement(ticker, as_of_date, "Cash Flow (annual)", "get_cash_flow")


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    if freq == "annual":
        return _statement(ticker, as_of_date, "Profit & Loss (annual)", "get_profit_loss")
    return _statement(ticker, as_of_date, "Quarterly Results", "get_quarterly_results")
