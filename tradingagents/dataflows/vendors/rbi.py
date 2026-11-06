"""Reserve Bank of India, scraped from rbi.org.in: policy rates, MPC decisions,
press releases and the Weekly Statistical Supplement.

The homepage "Current Rates" box is live only. Press releases (and with them
the MPC resolutions and the WSS extracts) come from the month-by-month press
release archive, so a run dated in the past reads what RBI had published by then.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

import requests

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import cached_json

SITE = "https://www.rbi.org.in"
_LIST = f"{SITE}/Scripts/BS_PressReleaseDisplay.aspx"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/130.0 Safari/537.36"}
_LIVE_TTL = 60 * 60
_ARCHIVE_TTL = 30 * 24 * 60 * 60
_MAX_CHARS = 6000

# Indicator names this vendor answers; anything else is left to the next vendor (FRED).
RATES = {"rbi_rates", "rbi_policy_rates", "repo_rate", "crr", "slr", "gsec_yields", "india_rates"}
MPC = {"rbi_mpc", "mpc", "rbi_policy", "rbi_monetary_policy"}
PRESS = {"rbi_press_releases", "rbi_news"}
WSS = {"rbi_wss", "forex_reserves", "india_forex_reserves", "india_money_supply", "m3", "bank_credit"}


def _get(url: str, data: dict | None = None) -> str:
    try:
        with requests.Session() as s:
            if data is None:
                response = s.get(url, headers=_UA, timeout=30)
            else:
                # The archive is an ASP.NET form: post back the page's own hidden fields.
                page = s.get(url, headers=_UA, timeout=30).text
                hidden = dict(re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page))
                response = s.post(url, data={**hidden, **data}, headers=_UA, timeout=30)
            response.raise_for_status()
            return response.text
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"rbi.org.in request failed: {exc}") from exc


def _text(fragment: str) -> str:
    """HTML as text, table rows on their own lines and cells split by ' | '."""
    fragment = re.sub(r"(?i)</t[dh]>", " | ", fragment)
    fragment = re.sub(r"(?i)</tr>|<br\s*/?>|</p>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip(" |") for line in text.splitlines())
    return "\n".join(line for line in lines if line.strip(" |"))


# --- current rates (homepage) --------------------------------------------------------

def current_rates() -> list[list[str]]:
    """The homepage "Current Rates" box as [section, name, value] rows."""
    def fetch():
        page = _get(f"{SITE}/")
        start, end = page.find("CURRENT RATES START"), page.find("CURRENT RATES END")
        if start < 0:
            raise VendorUnavailableError("rbi.org.in homepage has no Current Rates box")
        rows, section = [], ""
        for head, name, value in re.findall(
            r'role="button">([^<]+)</a>|<th[^>]*>\s*([^<]+?)\s*</th>\s*<td[^>]*>\s*:?\s*([^<]+?)\s*</td>',
            page[start:end if end > 0 else None],
        ):
            if head:
                section = html.unescape(head).replace("\xa0", " ")
                section = re.sub(r"\s+", " ", section).strip()
            else:
                rows.append([section, html.unescape(name).strip(), html.unescape(value).strip()])
        return rows

    return cached_json("rbi", "current-rates", _LIVE_TTL, fetch)


# --- press release archive -----------------------------------------------------------

def releases(year: int, month: int) -> list[list[str]]:
    """A month's press releases as [YYYY-MM-DD, prid, title], newest first."""
    def fetch():
        page = _get(_LIST, {"hdnYear": str(year), "hdnMonth": str(month)})
        out, day = [], ""
        for header, prid, title in re.findall(
            r'tableheader[^>]*><b>([^<]+)<|prid=(\d+)>([^<]+)<', page
        ):
            if header:
                day = datetime.strptime(header.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
            elif day:
                out.append([day, prid, html.unescape(title).strip()])
        return out

    current = datetime.now()
    ttl = _LIVE_TTL if (year, month) == (current.year, current.month) else _ARCHIVE_TTL
    return cached_json("rbi", f"releases-{year}-{month:02d}", ttl, fetch)


def _published_by(as_of_date: str, months: int) -> list[list[str]]:
    """Releases from the ``months`` months up to ``as_of_date``, dated by then."""
    day = datetime.strptime(as_of_date, "%Y-%m-%d")
    out = []
    for back in range(months):
        y, m = day.year, day.month - back
        while m < 1:
            y, m = y - 1, m + 12
        out += [r for r in releases(y, m) if r[0] <= as_of_date]
    return out


def release_text(prid: str) -> str:
    def fetch():
        page = _get(f"{_LIST}?prid={prid}")
        start = page.find('class="tablebg"')
        return _text(page[page.find(">", start) + 1:] if start >= 0 else page)

    return cached_json("rbi", f"release-{prid}", _ARCHIVE_TTL, fetch)


def _latest(as_of_date: str, pattern: str, months: int) -> list[str]:
    for row in _published_by(as_of_date, months):
        if re.search(pattern, row[2], re.IGNORECASE):
            return row
    raise NoMarketDataError("RBI", detail=f"no release matching {pattern!r} in the {months} months to {as_of_date}")


# --- the vendor entry point ----------------------------------------------------------

def get_macro_data(indicator: str, as_of_date: str, look_back_days: int | None = None) -> str:
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in RATES:
        if is_historical(as_of_date):
            return (f"# RBI current rates\nunavailable for historical date {as_of_date}: rbi.org.in shows "
                    f"only today's rates. Use 'rbi_mpc' for the policy decision in force on that date.")
        rows = current_rates()
        return "# RBI Current Rates (rbi.org.in, today)\n" + "\n".join(f"- {s}: {n} = {v}" for s, n, v in rows)
    if key in MPC:
        day, prid, title = _latest(as_of_date, r"Resolution of the Monetary Policy Committee", 4)
        return f"# {title} (RBI, {day})\n\n{release_text(prid)[:_MAX_CHARS]}"
    if key in WSS:
        day, prid, title = _latest(as_of_date, r"Weekly Statistical Supplement", 2)
        return (f"# {title} (RBI, {day})\nForex reserves (section 2), scheduled bank deposits and credit "
                f"(section 4) and money stock M3 (section 6), INR crore / USD million.\n\n"
                f"{release_text(prid)[:_MAX_CHARS]}")
    if key in PRESS:
        start = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=look_back_days or 14)).strftime("%Y-%m-%d")
        rows = [r for r in _published_by(as_of_date, 2) if r[0] >= start]
        return (f"# RBI press releases, {start} to {as_of_date}\n"
                + ("\n".join(f"- {d}: {t}" for d, _, t in rows[:60]) or "None."))
    raise NoMarketDataError(indicator, detail="not an RBI indicator")
