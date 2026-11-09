"""BetRivers — Kambi offering API that powers nj.betrivers.com."""
from __future__ import annotations

import requests

from ..models import Line, american_from_str
from .http import HEADERS

NAME = "BetRivers"
URL = "https://eu.offering-api.kambicdn.com/offering/v2018/rsius{state}/listView/{path}/all/all/matches.json"
PATHS = {
    "NBA": "basketball/nba", "NFL": "american_football/nfl", "NHL": "ice_hockey/nhl", "MLB": "baseball/mlb",
    "NCAAF": "american_football/ncaaf", "NCAAB": "basketball/ncaab",
    "WNCAAB": "basketball/ncaaw", "NCAABASE": "baseball/ncaa",
}
SUPPORTED = set(PATHS)
OUTCOME_SIDES = {"OT_ONE": "home", "OT_TWO": "away", "OT_OVER": "over", "OT_UNDER": "under"}


def _market(bet_offer: dict) -> str | None:
    kind = bet_offer.get("betOfferType", {}).get("englishName", "")
    label = bet_offer.get("criterion", {}).get("englishLabel", "")
    if kind == "Match":
        return "moneyline"
    if kind == "Handicap" and ("Spread" in label or "Run Line" in label or "Puck Line" in label or "Handicap" in label):
        return "spread"
    if kind == "Over/Under" and label.startswith("Total"):
        return "total"
    return None


def fetch(league: str, state: str = "nj", **_) -> list[Line]:
    resp = requests.get(
        URL.format(state=state, path=PATHS[league]),
        params={"lang": "en_US", "market": f"US-{state.upper()}", "client_id": 2,
                "channel_id": 1, "useCombined": "true"},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()

    lines: list[Line] = []
    for item in resp.json().get("events", []):
        ev = item["event"]
        if "MATCH" not in ev.get("tags", []):
            continue
        # englishName is "Home Team - Away Team" with full team names
        home, _, away = ev.get("englishName", "").partition(" - ")
        for bo in item.get("betOffers", []):
            market = _market(bo)
            if not market:
                continue
            for o in bo.get("outcomes", []):
                side = OUTCOME_SIDES.get(o.get("type"))
                odds = american_from_str(o.get("oddsAmerican"))
                if not side or odds is None or o.get("status", "OPEN") != "OPEN":
                    continue
                raw_line = o.get("line")
                lines.append(Line(
                    book=NAME, league=league, event_id=str(ev["id"]), start_time=ev["start"],
                    away_team=away or ev.get("awayName"), home_team=home or ev.get("homeName"),
                    market=market, side=side,
                    line=None if raw_line is None else raw_line / 1000,
                    odds=odds, live=ev.get("state") == "STARTED",
                ))
    return lines
