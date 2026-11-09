"""theScore Bet — loads the public competition page in headless Chromium and
reads the GraphQL responses the site requests for itself.

theScore uses persisted GraphQL queries whose hashes change with each deploy,
so reading the page's own traffic is sturdier than calling the API directly.
"""
from __future__ import annotations

from ..models import Line, american_from_str
from .browser import capture_json

NAME = "theScore Bet"
PAGES = {
    "NBA": "https://sportsbook.thescore.bet/sport/basketball/organization/united-states/competition/nba",
    "NFL": "https://sportsbook.thescore.bet/sport/football/organization/united-states/competition/nfl",
    "NHL": "https://sportsbook.thescore.bet/sport/hockey/organization/united-states/competition/nhl",
    "MLB": "https://sportsbook.thescore.bet/sport/baseball/organization/united-states/competition/mlb",
    "NCAAF": "https://sportsbook.thescore.bet/sport/football/organization/united-states/competition/ncaaf",
    "NCAAB": "https://sportsbook.thescore.bet/sport/basketball/organization/united-states/competition/ncaab",
    "WNCAAB": "https://sportsbook.thescore.bet/sport/basketball/organization/united-states/competition/wncaab",
    # No college baseball page is listed in theScore's menu (checked in the off-season).
}
SUPPORTED = set(PAGES)
MARKET_TYPES = {"MONEYLINE": "moneyline", "SPREAD": "spread", "TOTAL": "total"}
SIDES = {
    "AWAY_MONEYLINE": "away", "HOME_MONEYLINE": "home",
    "AWAY_SPREAD": "away", "HOME_SPREAD": "home",
    "OVER": "over", "UNDER": "under",
}


def _cards(obj, out: list):
    if isinstance(obj, dict):
        if obj.get("__typename") == "GridMarketCard":
            out.append(obj)
        for v in obj.values():
            _cards(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _cards(v, out)


def _team(participant: dict | None) -> str:
    return (participant or {}).get("fullName") or (participant or {}).get("name") or ""


def fetch(league: str, **_) -> list[Line]:
    payloads = capture_json(PAGES[league], url_contains="/graphql/", scrolls=4)
    cards: list[dict] = []
    for p in payloads:
        _cards(p, cards)

    lines: dict[tuple, Line] = {}
    for card in cards:
        ev = card.get("fallbackEvent") or (card.get("event") or {}).get("fallbackEvent") or {}
        if not ev:
            continue
        away, home = _team(ev.get("awayParticipant")), _team(ev.get("homeParticipant"))
        if not (away and home):
            away, _, home = ev.get("name", "").partition(" @ ")
        for m in card.get("pagedMarkets") or []:
            market = MARKET_TYPES.get(m.get("type"))
            if not market or m.get("status") != "OPEN":
                continue
            for s in m.get("selections") or []:
                side = SIDES.get(s.get("type"))
                odds = american_from_str((s.get("odds") or {}).get("formattedOdds"))
                if not side or odds is None or s.get("status") != "OPEN":
                    continue
                pts = (s.get("points") or {}).get("decimalPoints")
                line = Line(
                    book=NAME, league=league, event_id=ev["id"].split(":")[-1],
                    start_time=ev.get("startTime", ""), away_team=away, home_team=home,
                    market=market, side=side, line=None if market == "moneyline" else pts,
                    odds=odds, live=ev.get("status") == "IN_PLAY",
                )
                # The page can return the same card more than once; keep the last copy.
                lines[(line.event_id, market, side)] = line
    return list(lines.values())
