"""Run one analysis for the VS Code extension, reporting progress as JSON lines on stdout.

Events: ``start``, ``tool`` (an agent called a data tool), ``section`` (a report
section is ready), ``done`` (rating, report path, final decision) and ``error``.
Everything else the run prints goes to stderr, so stdout carries only events.
"""

from __future__ import annotations

import argparse
import json
import sys

ANALYSTS = ("market", "social", "news", "fundamentals")
SECTIONS = {
    "market_report": "Market Analyst",
    "sentiment_report": "Sentiment Analyst",
    "news_report": "News Analyst",
    "fundamentals_report": "Fundamentals Analyst",
    "investment_plan": "Research Manager",
    "trader_investment_plan": "Trader",
    "final_trade_decision": "Portfolio Manager",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tradingagents.vscode_run")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--analysts", default=",".join(ANALYSTS))
    parser.add_argument("--market", default=None)
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--language", default="English")
    parser.add_argument("--out", required=True, help="Directory the report is written to")
    args = parser.parse_args(argv)

    out = sys.stdout
    sys.stdout = sys.stderr

    def emit(event: str, **data) -> None:
        out.write(json.dumps({"event": event, **data}) + "\n")
        out.flush()

    try:
        from tradingagents.agents.rating import run_rating
        from tradingagents.dataflows.symbols import crypto_base, market_symbol
        from tradingagents.default_config import build_default_config
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = build_default_config(args.market)
        config.update(max_debate_rounds=args.depth, max_risk_discuss_rounds=args.depth,
                      output_language=args.language)
        ticker = market_symbol(args.ticker, config["market"])
        asset_type = "crypto" if crypto_base(ticker) else "stock"
        analysts = [a.strip().replace("sentiment", "social") for a in args.analysts.split(",") if a.strip()]
        if asset_type == "crypto":
            analysts = [a for a in analysts if a != "fundamentals"]
        unknown = sorted(set(analysts) - set(ANALYSTS))
        if unknown or not analysts:
            raise ValueError(f"unknown analysts {unknown}; choose from market, sentiment, news, fundamentals")

        graph = TradingAgentsGraph([a for a in ANALYSTS if a in analysts], config=config)
        emit("start", ticker=ticker, date=args.date, analysts=analysts, market=config["market"])

        state, seen_sections, seen_calls = {}, set(), set()
        run_state = graph.create_run_state(ticker, args.date, asset_type)
        for messages, chunk in graph.stream_run(run_state, **graph.propagator.get_graph_args()):
            for message in messages:
                for call in getattr(message, "tool_calls", None) or []:
                    if call["id"] not in seen_calls:
                        seen_calls.add(call["id"])
                        emit("tool", name=call["name"])
            if not chunk:
                continue
            state.update(chunk)
            for key, agent in SECTIONS.items():
                if state.get(key) and key not in seen_sections:
                    seen_sections.add(key)
                    emit("section", key=key, agent=agent)

        graph.record_decision(ticker, args.date, state)
        graph.save_reports(state, ticker, save_path=args.out)
        emit("done", rating=run_rating(state), report=args.out,
             decision=state.get("final_trade_decision", ""))
        return 0
    except Exception as exc:  # noqa: BLE001 — every failure is reported to the extension
        emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        sys.stdout = out


if __name__ == "__main__":
    sys.exit(main())
