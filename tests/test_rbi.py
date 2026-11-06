"""RBI vendor: the current-rates box, the press-release archive, and point-in-time reads."""

from __future__ import annotations

import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import rbi

HOME = """<!-- CURRENT RATES START--><a role="button">Policy&nbsp; Rates</a>
<table><tr><th> Policy Repo Rate </th><td> : 5.50% </td></tr></table>
<a role="button">Reserve &nbsp;Ratios</a><table><tr><th> CRR </th><td> : 3.00% </td></tr></table>
<!-- CURRENT RATES END-->"""

OCTOBER = """<td class="tableheader"><b>Oct 30, 2026<b></td>
<a class='link2' href=BS_PressReleaseDisplay.aspx?prid=3>Reserve Bank of India \u2013 Bulletin Weekly Statistical Supplement \u2013 Extract</a>
<td class="tableheader"><b>Oct 07, 2026<b></td>
<a class='link2' href=BS_PressReleaseDisplay.aspx?prid=2>Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee October 5 to 7, 2026</a>"""
AUGUST = """<td class="tableheader"><b>Aug 05, 2026<b></td>
<a class='link2' href=BS_PressReleaseDisplay.aspx?prid=1>Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee August 3 to 5, 2026</a>"""
RELEASE = {
    "1": '<table class="tablebg"><tr><td>The MPC voted to keep the repo rate at 5.25 per cent.</td></tr></table>',
    "2": '<table class="tablebg"><tr><td>The MPC voted to increase the repo rate by 25 bps to 5.50 per cent.</td></tr></table>',
    "3": '<table class="tablebg"><tr><td>1 Total Reserves</td><td>6808728</td><td>717056</td></tr></table>',
}


@pytest.fixture
def site(monkeypatch):
    def get(url, data=None):
        if url == f"{rbi.SITE}/":
            return HOME
        if data is not None:
            return {"10": OCTOBER, "8": AUGUST}.get(data["hdnMonth"], "")
        return RELEASE[url.rsplit("=", 1)[1]]

    monkeypatch.setattr(rbi, "_get", get)
    monkeypatch.setattr(rbi, "cached_json", lambda namespace, key, ttl, fetch: fetch())


@pytest.mark.unit
def test_todays_rates_come_from_the_homepage_box(site, monkeypatch):
    monkeypatch.setattr(rbi, "is_historical", lambda d: False)
    out = rbi.get_macro_data("repo_rate", "2026-11-06")
    assert "Policy Rates: Policy Repo Rate = 5.50%" in out and "Reserve Ratios: CRR = 3.00%" in out


@pytest.mark.unit
def test_a_past_run_gets_no_live_rates(site):
    assert "unavailable for historical date 2020-01-01" in rbi.get_macro_data("rbi_rates", "2020-01-01")


@pytest.mark.unit
def test_the_mpc_decision_is_the_one_in_force_on_the_run_date(site):
    assert "increase the repo rate by 25 bps" in rbi.get_macro_data("rbi_mpc", "2026-10-31")
    before = rbi.get_macro_data("rbi_mpc", "2026-10-06")
    assert "keep the repo rate at 5.25" in before and "increase" not in before


@pytest.mark.unit
def test_the_weekly_supplement_reads_as_table_rows(site):
    assert "1 Total Reserves | 6808728 | 717056" in rbi.get_macro_data("forex_reserves", "2026-10-31")


@pytest.mark.unit
def test_press_releases_are_listed_for_the_window(site):
    out = rbi.get_macro_data("rbi_press_releases", "2026-10-31", 7)
    assert "2026-10-30" in out and "2026-10-07" not in out


@pytest.mark.unit
def test_other_indicators_are_left_to_fred(monkeypatch):
    with pytest.raises(NoMarketDataError):
        rbi.get_macro_data("cpi", "2026-10-31")
    monkeypatch.setitem(router.VENDOR_METHODS["get_macro_indicators"], "fred", lambda *a, **k: "fred cpi")
    assert router.route_to_vendor("get_macro_indicators", "cpi", "2026-10-31", None) == "fred cpi"


@pytest.mark.integration
def test_live_rbi_rates():
    assert "Policy Repo Rate" in rbi.get_macro_data("rbi_rates", rbi.datetime.now().strftime("%Y-%m-%d"))
