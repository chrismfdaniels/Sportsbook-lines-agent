"""FanDuel — public content-managed-page API used by sportsbook.fanduel.com."""
from __future__ import annotations

import re

import requests

from ..models import Line
from .http import HEADERS

NAME = "FanDuel"
# _ak is the public app key embedded in FanDuel's web client.
URL = "https://sbapi.{state}.sportsbook.fanduel.com/api/content-managed-page"
PAGE_IDS = {
    "NBA": "nba", "NFL": "nfl", "NHL": "nhl", "MLB": "mlb",
    "NCAAF": "ncaaf", "NCAAB": "ncaab", "WNCAAB": "ncaaw", "NCAABASE": "ncaabaseball",
}
SUPPORTED = set(PAGE_IDS)
# Pro pages also list eBasketball (NBA 2K) games and futures, so pin those to the
# real competition. College competition names vary ("NCAA Football Games"), so
# for those just drop futures and esports.
PRO_COMPETITIONS = {"NBA": "NBA", "NFL": "NFL", "NHL": "NHL", "MLB": "MLB"}


def _wanted_competition(league: str, comp_name: str) -> bool:
    if league in PRO_COMPETITIONS:
        return comp_name.upper() == PRO_COMPETITIONS[league]
    low = comp_name.lower()
    if "futures" in low or low.startswith("ebasketball"):
        return False
    # The men's basketball page also lists women's games ("NCAA Basketball Women's Games")
    if league == "NCAAB":
        return "women" not in low
    return True


MARKET_TYPES = {
    "MONEY_LINE": "moneyline",
    "MATCH_HANDICAP_(2-WAY)": "spread",
    "TOTAL_POINTS_(OVER/UNDER)": "total",
}


def fetch(league: str, state: str = "nj", **_) -> list[Line]:
    resp = requests.get(
        URL.format(state=state),
        params={"page": "CUSTOM", "customPageId": PAGE_IDS[league],
                "_ak": "FhMFpcPWXMeyZxOx", "timezone": "America/New_York"},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    att = resp.json().get("attachments", {})
    events = att.get("events", {})
    comps = att.get("competitions", {})

    lines: list[Line] = []
    for m in att.get("markets", {}).values():
        market = MARKET_TYPES.get(m.get("marketType"))
        ev = events.get(str(m.get("eventId")))
        if not market or not ev or " @ " not in ev.get("name", "") or m.get("marketStatus") != "OPEN":
            continue
        if not _wanted_competition(league, comps.get(str(ev.get("competitionId")), {}).get("name", "")):
            continue
        # MLB names carry the probable pitcher: "Milwaukee Brewers (R Gasser)"
        away, home = (re.sub(r"\s*\(.*\)$", "", s).strip() for s in ev["name"].split(" @ ", 1))
        for r in m.get("runners", []):
            if r.get("runnerStatus") != "ACTIVE":
                continue
            odds = (r.get("winRunnerOdds", {}).get("americanDisplayOdds") or {}).get("americanOddsInt")
            side = (r.get("result") or {}).get("type", "").lower()
            if odds is None or side not in ("away", "home", "over", "under"):
                continue
            lines.append(Line(
                book=NAME, league=league, event_id=str(ev["eventId"]), start_time=ev["openDate"],
                away_team=away, home_team=home, market=market, side=side,
                line=None if market == "moneyline" else r.get("handicap"),
                odds=int(odds), live=bool(m.get("inPlay")),
            ))
    return lines
