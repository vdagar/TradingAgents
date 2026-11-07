"""The VS Code extension's run entry: JSON-line events on stdout and the saved report."""

from __future__ import annotations

import json

import pytest

from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import vscode_run
from tradingagents.graph import trading_graph


def _events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.unit
def test_a_run_streams_its_progress_and_saves_the_report(tmp_path, monkeypatch, capsys, offline):  # noqa: F811
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))

    code = vscode_run.main(["--ticker", "reliance", "--date", TRADE_DATE, "--analysts", "market,sentiment",
                            "--out", str(tmp_path / "report")])

    events = _events(capsys)
    assert code == 0
    assert events[0] == {"event": "start", "ticker": "RELIANCE.NS", "date": TRADE_DATE,
                         "analysts": ["market", "social"], "market": "IN"}
    assert {e["name"] for e in events if e["event"] == "tool"} >= {"get_stock_data"}
    assert {e["agent"] for e in events if e["event"] == "section"} >= {"Market Analyst", "Portfolio Manager"}
    done = events[-1]
    assert done["event"] == "done" and done["rating"] == "Overweight"
    assert (tmp_path / "report" / "complete_report.md").exists()
    assert done["report"] == str(tmp_path / "report")


@pytest.mark.unit
def test_a_failure_is_one_error_event(tmp_path, capsys):
    code = vscode_run.main(["--ticker", "TCS", "--date", TRADE_DATE, "--analysts", "macro",
                            "--out", str(tmp_path)])
    assert code == 1
    (event,) = _events(capsys)
    assert event["event"] == "error" and "unknown analysts" in event["message"]
